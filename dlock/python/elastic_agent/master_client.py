# Copyright 2022 The DLRover Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib
import os
import socket
import threading
import time
from contextlib import closing
from typing import Dict, Optional

from dlock.proto import elastic_training_pb2, elastic_training_pb2_grpc
from dlock.python.common import env_utils, grpc
from dlock.python.common.constants import (
    JobConstant,
    NetworkFailureReason,
    NodeEnv,
    NodeEventType,
)
from dlock.python.common.log import default_logger as logger
from dlock.python.common.singleton import Singleton
from dlock.python.diagnosis.common.diagnosis_action import (
    DiagnosisAction,
    NoAction,
)
from dlock.python.diagnosis.common.diagnosis_data import DiagnosisData


def retry_grpc_request(func):
    def wrapper(self, *args, **kwargs):
        retry = kwargs.get("retry", 10)
        exception = None
        for i in range(retry):
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                class_name = self.__class__.__name__
                func_name = func.__name__
                logger.warning(
                    f"Retry {i} to {class_name}.{func_name} with failure {e}",
                )
                exception = e
                time.sleep(5)
        if exception:
            logger.error(exception)
            raise exception

    return wrapper


class MasterClient(Singleton):
    """MasterClient provides some APIs connect with the master
    service via gRPC call.
    Args:
        master_addr: the master address
        node_id (int), the unique and ordered node ID assigned
        by dlock command-line.
        node_type: the job type of node contains "worker", "ps"
            "evaluator" and "chief".
        timeout (int): the timeout second of grpc requests.

    Examples::
        channel = elasticai_api.util.grpc_utils.build_channel(
            "localhost:50001"
        )
        mc = MasterClient(channel, work_id=0)
        # get task unit from master service
        mc.get_task(...)
    """

    _instance_lock = threading.Lock()

    def __init__(self, master_addr, node_id, node_type, timeout=5):
        logger.info(
            f"Build master client with master_addr: {master_addr}, "
            f"node_id: {node_id}, node_type: {node_type}."
        )
        self._timeout = timeout
        self._master_addr = master_addr
        self._channel = grpc.build_channel(master_addr)
        self._stub = elastic_training_pb2_grpc.MasterStub(self._channel)
        self._node_id = node_id
        self._node_type = node_type
        self._node_ip = os.getenv("NODE_IP", "")
        self._worker_local_process_id = int(os.getenv("LOCAL_RANK", 0))
        self._ddp_server_port = self.find_free_port()

        self._diagnosis_action_module = importlib.import_module(
            "dlock.python.diagnosis.common.diagnosis_action"
        )

    def __del__(self):
        if self._channel:
            self._channel.close()

    def close_channel(self):
        if self._channel:
            self._channel.close()

    def open_channel(self):
        self._channel = grpc.build_channel(self._master_addr)
        self._stub = elastic_training_pb2_grpc.MasterStub(self._channel)

    def find_free_port(self):
        with closing(
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("localhost", 0))
            _, port = sock.getsockname()
            return port

    @retry_grpc_request
    def _report(self, message: grpc.Message):
        request = elastic_training_pb2.Message()
        request.node_id = self._node_id
        request.node_type = self._node_type
        request.data = message.serialize()
        return self._stub.report(request, timeout=self._timeout)

    @retry_grpc_request
    def _get(self, message: grpc.Message):
        request = elastic_training_pb2.Message()
        request.node_id = self._node_id
        request.node_type = self._node_type
        request.data = message.serialize()
        response = self._stub.get(request, timeout=self._timeout)
        res_message = grpc.deserialize_message(response.data)
        return res_message

    def kv_store_set(self, key, value):
        message = grpc.KeyValuePair(key, value)
        response = self._report(message)
        return response.success

    def kv_store_get(self, key):
        request = grpc.KeyValuePair(key)
        result: grpc.KeyValuePair = self._get(request)
        return result.value

    def get_task(self, dataset_name):
        """Get a task from master.

        Args:
            dataset_name: string
            the training phase, c.f. /dlock/proto/dlock.proto

        Returns:
            the task unit assigned by master,
            c.f. /dlock/proto/dlock.proto
        """

        req = grpc.TaskRequest(dataset_name)

        success = False
        res = None
        exception = None
        for _ in range(10):
            try:
                res = self._get(req)
                success = True
                break
            except Exception as e:
                exception = e
                time.sleep(15)
        if not success:
            logger.warning(exception)
        if not res:
            res = grpc.Task()
        return success, res

    def report_task_result(self, dataset_name, task_id, err_msg):
        """Report task result to master.

        Args:
          task_id: int
          the task ID assigned by master

          err_msg: string
          the error message on training.
        """
        message = grpc.TaskResult(dataset_name, task_id, err_msg)
        return self._report(message)

    def report_dataset_shard_params(
        self,
        batch_size,
        num_epochs=None,
        dataset_size=None,
        shuffle=False,
        num_minibatches_per_shard=0,
        dataset_name=None,
        task_type=elastic_training_pb2.NONE,
        storage_type="",
    ):
        message = grpc.DatasetShardParams(
            batch_size=batch_size,
            num_epochs=num_epochs,
            dataset_size=dataset_size,
            shuffle=shuffle,
            num_minibatches_per_shard=num_minibatches_per_shard,
            dataset_name=dataset_name,
            task_type=task_type,
            storage_type=storage_type,
        )
        return self._report(message)

    def ready_for_ps_relaunch(self):
        message = grpc.PsReady()
        return self._report(message)

    def get_shard_checkpoint(self, dataset_name):
        req = grpc.ShardCheckpointRequest(dataset_name)
        res: grpc.ShardCheckpoint = self._get(req)
        return res.content

    def report_shard_checkpoint(self, shard_checkpoint):
        request = grpc.ShardCheckpoint(shard_checkpoint)
        return self._report(request)

    def report_used_resource(self, memory, cpu, gpu_stats):
        message = grpc.ResourceStats(memory, cpu, gpu_stats)
        return self._report(message)

    def report_model_info(self, model_info):
        self._report(model_info)

    def report_global_step(
        self, global_step, timestamp, elapsed_time_per_step=0
    ):
        message = grpc.GlobalStep(
            timestamp=timestamp,
            step=global_step,
            elapsed_time_per_step=elapsed_time_per_step,
        )
        return self._report(message)

    def report_heart_beat(self, timestamp) -> DiagnosisAction:
        message = grpc.HeartBeat(timestamp=timestamp)
        response: grpc.HeartbeatResponse = self._get(message)
        action = NoAction()

        if not response:
            logger.warning("No response from heartbeat reporting.")
            return action

        action_cls: Optional[DiagnosisData] = getattr(
            self._diagnosis_action_module,
            response.action.action_cls,
        )
        if action_cls is None:
            logger.warning(
                "Invalid diagnosis action "
                f"action type: {response.action.action_cls}"
            )
        else:
            action = action_cls.from_json(response.action.action_content)
        return action

    def get_cluster_version(self, version_type, task_type, task_id):
        request = grpc.ClusterVersionRequest(
            task_type=task_type,
            task_id=task_id,
            version_type=version_type,
        )
        result: grpc.ClusterVersion = self._get(request)
        return result.version

    def update_node_addr(self, task_type, task_id, node_addr):
        message = grpc.NodeAddress(type=task_type, id=task_id, addr=node_addr)
        res = self._report(message)
        return res

    def report_node_event(
        self,
        event_type,
        event_msg="",
        event_time=0,
        event_elapsed_time=0,
        node_rank=-1,
    ):
        message = grpc.NodeEvent(
            event_type=event_type,
            event_message=event_msg,
            event_time=event_time,
            event_elapsed_time=event_elapsed_time,
            node=grpc.NodeMeta(
                type=self._node_type, id=self._node_id, addr=self._node_ip
            ),
        )

        if node_rank != -1:
            message.node.rank = node_rank

        return self._report(message)

    def report_network_check_status(self, node_rank, status, elapsed_time):
        return self.report_node_event(
            event_type=status,
            event_elapsed_time=elapsed_time,
            node_rank=node_rank,
        )

    def report_failed_exited(self):
        return self.report_node_event(NodeEventType.FAILED_EXITED)

    def report_succeeded_exited(self):
        return self.report_node_event(NodeEventType.SUCCEEDED_EXITED)

    def update_cluster_version(
        self, version_type, version, task_type, task_id
    ):
        message = grpc.ClusterVersion(
            task_type=task_type,
            task_id=task_id,
            version_type=version_type,
            version=version,
        )
        self._report(message)

    def query_ps_nodes(self):
        request = grpc.PsNodesRequest()
        result: grpc.PsNodes = self._get(request)
        return result.nodes, result.ps_failure

    def query_training_status(self):
        request = grpc.TrainingStatusRequest()
        response: grpc.TrainingStatus = self._get(request)
        return response.status

    def join_sync(self, sync_name):
        message = grpc.SyncJoin(sync_name)
        logger.info(
            " {}:{} join sync {}".format(
                self._node_id, self._node_type, sync_name
            )
        )
        response = self._report(message)
        return response.success

    def sync_finished(self, sync_name):
        message = grpc.SyncFinish(sync_name)
        response = self._report(message)
        return response.success

    def barrier(self, barrier_name, notify=False):
        message = grpc.SyncBarrier(barrier_name, notify)
        response = self._report(message)
        return response.success

    def get_running_nodes(self):
        request = grpc.RunningNodesRequest()
        result: grpc.RunningNodes = self._get(request)
        return result.nodes

    def num_nodes_waiting(self, rdzv_name):
        request = grpc.WaitingNodeNumRequest(rdzv_name=rdzv_name)
        try:
            result: grpc.RendezvousState = self._get(request)
            return result.waiting_num
        except Exception:
            logger.warning("Fail to query the number of waiting nodes.")
            return 0

    def join_rendezvous(self, node_rank, local_world_size, rdzv_name=""):
        request = grpc.JoinRendezvousRequest(
            node_id=self._node_id,
            node_rank=node_rank,
            local_world_size=local_world_size,
            rdzv_name=rdzv_name,
            node_ip=self._node_ip,
        )
        result: grpc.RendezvousState = self._get(request)
        return result.round

    def get_comm_world(self, rdzv_name, node_rank):
        request = grpc.CommWorldRequest(node_id=node_rank, rdzv_name=rdzv_name)
        result: grpc.RendezvousState = self._get(request)
        return result.round, result.group, result.world

    def check_fault_node(self, timeout=300):
        request = grpc.NetworkReadyRequest()
        start = time.time()
        while True:
            result: grpc.NetworkCheckResult = self._get(request)
            if (
                result.reason == NetworkFailureReason.WAITING_NODE
                or result.reason == NetworkFailureReason.NO_INIT
            ) and time.time() - start < timeout:
                time.sleep(JobConstant.MASTER_CLIENT_CHECK_FAULT_SLEEP_TIMEOUT)
                continue
            break
        return result.nodes, result.reason

    def check_straggler(self, timeout=300):
        request = grpc.StragglerExistRequest()
        start = time.time()
        while True:
            result: grpc.NetworkCheckResult = self._get(request)
            if (
                result.reason == NetworkFailureReason.WAITING_NODE
                and time.time() - start < timeout
            ):
                time.sleep(
                    JobConstant.MASTER_CLIENT_CHECK_STRAGGLER_SLEEP_TIMEOUT
                )
                continue
            break
        return result.nodes, result.reason

    def report_rdzv_params(
        self, min_nodes, max_nodes, waiting_timeout, node_unit, joint_timeout
    ):
        message = grpc.RendezvousParams(
            min_nodes,
            max_nodes,
            waiting_timeout,
            node_unit,
            joint_timeout,
        )
        response = self._report(message)
        return response.success

    def report_failures(self, error_data, restart_count=-1, level=""):
        message = grpc.NodeFailure(error_data, restart_count, level)
        self._report(message)

    def report_paral_config(self, config: grpc.ParallelConfig):
        self._report(config)

    def report_diagnosis_agent_metrics(self, data: DiagnosisData):
        message = grpc.DiagnosisReportData(
            data.__class__.__name__,
            data.to_json(),
            data.node_rank,
        )
        self._report(message)

    def get_paral_config(self) -> grpc.ParallelConfig:
        request = grpc.ParallelConfigRequest()
        result = self._get(request)
        return result

    def need_to_restart_training(self):
        request = grpc.CheckHardwareResetRequest()
        try:
            result: grpc.ParallelConfig = self._get(request)
            return result.restart
        except Exception:
            logger.warning("Fail to verify restarting training processes.")
            return False

    def sync_checkpoint(self, step):
        request = grpc.NodeCheckpointState()
        request.step = step
        response = self._report(request)
        return response.success

    def sync_training_ports(self, port) -> grpc.SyncTrainingPort:
        request = grpc.SyncTrainingPort(port=port)
        response: grpc.SyncTrainingPort = self._get(request)
        return response

    def get_elastic_run_config(self) -> Dict[str, str]:
        request = grpc.ElasticRunConfigRequest()
        response: grpc.ElasticRunConfig = self._get(request)
        return response.configs

    def report_event(
        self,
        event_type: str = "",
        instance: str = "",
        action: str = "",
        msg: str = "",
        labels: Optional[Dict[str, str]] = None,
    ):
        if labels is None:
            labels = {}
        message = grpc.Event(
            event_type=event_type,
            instance=instance,
            action=action,
            msg=msg,
            labels=labels,
        )
        self._report(message)

    @classmethod
    def singleton_instance(cls, *args, **kwargs):
        if not cls._instance:
            with cls._instance_lock:
                if not cls._instance:
                    cls._instance = build_master_client(*args, **kwargs)
        return cls._instance


def build_master_client(
    master_addr=None, timeout=JobConstant.MASTER_CLIENT_GRPC_DEFAULT_TIMEOUT
):
    """
    Build a master client.

    Args:
        master_addr (str): the address of the job master, the format
            is "{IP}:{PORT}"
        timeout (int): the timeout second of grpc requests.
    """
    if master_addr is None:
        master_addr = os.getenv(NodeEnv.DLOCK_MASTER_ADDR, "")
    node_id = env_utils.get_node_id()
    node_type = env_utils.get_node_type()

    try:
        _timeout = int(os.getenv(NodeEnv.MASTER_CLIENT_TIMEOUT, ""))
        logger.info(f"set master_client timeout to env {_timeout}")
    except Exception:
        _timeout = timeout
        logger.info(f"set master_client timeout to {_timeout}")

    master_client = None
    logger.info(f"Build master client with addr {master_addr}.")
    if master_addr:
        try:
            master_client = MasterClient(
                master_addr, node_id, node_type, timeout
            )
        except Exception:
            logger.info("The master is not available now.")
    return master_client
