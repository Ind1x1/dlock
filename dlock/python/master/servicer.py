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
import threading
import time
from concurrent import futures
from typing import Dict, List, Optional

import grpc as grpc_lib

from dlock.proto import elastic_training_pb2, elastic_training_pb2_grpc
from dlock.python.common import grpc
from dlock.python.common.constants import (
    GRPC,
    CustomMetricKeys,
    JobConstant,
    NodeEventType,
    NodeType,
    RendezvousName,
    TrainingExceptionLevel,
    TrainingLoopStatus,
)
from dlock.python.common.global_context import Context
from dlock.python.common.log import default_logger as logger
from dlock.python.diagnosis.common.diagnosis_data import DiagnosisData
from dlock.python.master.diagnosis.diagnosis_manager import DiagnosisManager
from dlock.python.master.elastic_training.kv_store_service import (
    KVStoreService,
)
from dlock.python.master.elastic_training.rdzv_manager import (
    NetworkCheckRendezvousManager,
    RendezvousManager,
)
from dlock.python.master.monitor.speed_monitor import SpeedMonitor
from dlock.python.master.node.job_manager import JobManager
from dlock.python.master.node.training_node import SyncNodeTrainingPorts
from dlock.python.master.shard.dataset_splitter import new_dataset_splitter
from dlock.python.master.shard.task_manager import TaskManager
from dlock.python.master.stats.job_collector import JobMetricCollector
from dlock.python.master.watcher.base_watcher import Node, NodeEvent
from dlock.python.util.queue.queue import RayEventQueue

try:
    from dlock.python.master.elastic_training.elastic_ps import (
        ElasticPsService,
    )
    from dlock.python.master.elastic_training.sync_service import SyncService
except ImportError:
    logger.info("Run the master locally.")
    pass


_dlock_context = Context.singleton_instance()
_DEFAULT_NUM_MINIBATCHES_PER_SHARD = 100
ray_event_queue = RayEventQueue.singleton_instance()


class MasterServicer(elastic_training_pb2_grpc.MasterServicer):
    """Master service implementation"""

    def __init__(
        self,
        task_manager,
        job_manager,
        speed_monitor: SpeedMonitor,
        rdzv_managers: Dict[str, RendezvousManager],
        diagnosis_manager: DiagnosisManager,
        job_metric_collector=None,
        elastic_ps_service=None,
        sync_service=None,
        error_monitor=None,
    ):
        self._task_manager: TaskManager = task_manager
        self._job_manager: JobManager = job_manager
        self._speed_monitor = speed_monitor
        self._rdzv_managers = rdzv_managers
        self._diagnosis_manager = diagnosis_manager
        self._kv_store = KVStoreService()
        self._job_metric_collector: JobMetricCollector = job_metric_collector
        self._elastic_ps_service: ElasticPsService = elastic_ps_service
        self._sync_service: SyncService = sync_service
        self._lock = threading.Lock()
        self._version = 0
        self._start_training_time = 0
        self._start_autoscale = False
        self._error_monitor = error_monitor

        # preload module for class reflection
        self._diagnosis_data_module = importlib.import_module(
            "dlock.python.diagnosis.common.diagnosis_data"
        )
        # clear kv store in case previous data is still there
        self._kv_store.clear()

    def get(self, request, _):
        node_type = request.node_type
        node_id = request.node_id
        req_message = grpc.deserialize_message(request.data)

        response = elastic_training_pb2.Message()
        if not req_message:
            return response
        message = None
        if isinstance(req_message, grpc.TaskRequest):
            message = self._get_task(node_type, node_id, req_message)
        elif isinstance(req_message, grpc.ShardCheckpointRequest):
            message = self._get_shard_checkpoint(req_message)
        elif isinstance(req_message, grpc.ClusterVersionRequest):
            message = self._get_cluster_version(req_message)
        elif isinstance(req_message, grpc.RunningNodesRequest):
            message = self._get_running_nodes()
        elif isinstance(req_message, grpc.JoinRendezvousRequest):
            message = self._join_rendezvous(req_message)
        elif isinstance(req_message, grpc.WaitingNodeNumRequest):
            message = self._num_nodes_waiting(req_message.rdzv_name)
        elif isinstance(req_message, grpc.NetworkReadyRequest):
            message = self._check_fault_node()
        elif isinstance(req_message, grpc.StragglerExistRequest):
            message = self._check_straggler()
        elif isinstance(req_message, grpc.CommWorldRequest):
            message = self._get_comm_world(req_message)
        elif isinstance(req_message, grpc.KeyValuePair):
            message = self._kv_store_get(req_message)
        elif isinstance(req_message, grpc.PsNodesRequest):
            message = self._query_ps_nodes()
        elif isinstance(req_message, grpc.TrainingStatusRequest):
            message = self._get_training_status()
        elif isinstance(req_message, grpc.ParallelConfigRequest):
            message = self._get_paral_config()
        elif isinstance(req_message, grpc.CheckHardwareResetRequest):
            message = self._need_to_restart_training(node_type, node_id)
        elif isinstance(req_message, grpc.SyncTrainingPort):
            message = self._sync_training_ports(node_id, req_message)
        elif isinstance(req_message, grpc.ElasticRunConfigRequest):
            configs = self._job_manager.get_elastic_run_configs()
            message = grpc.ElasticRunConfig(configs=configs)
        elif isinstance(req_message, grpc.HeartBeat):
            message = self._report_heartbeat(node_type, node_id, req_message)

        if message:
            response.data = message.serialize()
        return response

    def _get_task(self, node_type, node_id, request: grpc.TaskRequest):
        if not self._start_training_time:
            self._start_training_time = int(time.time())
        shard = grpc.Shard()
        res = grpc.Task(shard=shard)
        ds_name = request.dataset_name
        dataset = self._task_manager.get_dataset(ds_name)
        if not dataset:
            return res
        task = self._task_manager.get_dataset_task(node_type, node_id, ds_name)

        if task:
            res.task_id = task.task_id
            res.type = task.task_type
            res.shard.name = task.shard.name
            res.shard.start = task.shard.start
            res.shard.end = task.shard.end
            if task.shard.record_indices:
                res.shard.indices = task.shard.record_indices
        elif not dataset.completed():
            res.type = elastic_training_pb2.WAIT
        with self._lock:
            self._task_manager.reset_worker_start_task_time(node_id)
        return res

    def _get_shard_checkpoint(self, request: grpc.ShardCheckpointRequest):
        response = grpc.ShardCheckpoint()
        dataset = self._task_manager.get_dataset(request.dataset_name)
        checkpoint = dataset.checkpoint()
        if checkpoint:
            response.content = checkpoint.to_json()
        return response

    def _get_cluster_version(self, request: grpc.ClusterVersionRequest):
        message = grpc.ClusterVersion()
        if not self._elastic_ps_service:
            return message

        if request.task_type == NodeType.WORKER:
            message.version = self._elastic_ps_service.get_worker_version(
                request.version_type, request.task_id
            )
        elif request.task_type == NodeType.PS:
            message.version = self._elastic_ps_service.get_ps_version(
                request.version_type, request.task_id
            )
        return message

    def _query_ps_nodes(self):
        res = grpc.PsNodes(nodes=[])
        training_ps: List[Node] = self._job_manager.get_next_cluster_ps()
        ready = self._job_manager.ready_for_new_ps_cluster()
        ps_failure = self._job_manager.has_ps_failure()
        for ps in training_ps:
            ps_meta = grpc.NodeMeta()
            ps_meta.type = NodeType.PS
            ps_meta.addr = ps.service_addr
            ps_meta.cpu = ps.config_resource.cpu
            ps_meta.memory = int(ps.config_resource.memory)
            res.nodes.append(ps_meta)
        res.new_ps_ready = ready
        res.ps_failure = ps_failure
        return res

    def _get_running_nodes(self):
        res = grpc.RunningNodes(nodes=[])
        nodes: List[Node] = self._job_manager.get_running_nodes()
        for node in nodes:
            meta = grpc.NodeMeta()
            meta.type = node.type
            meta.addr = node.service_addr
            meta.cpu = node.config_resource.cpu
            meta.memory = node.config_resource.memory
            if node.config_resource.gpu_type:
                meta.gpu_type = node.config_resource.gpu_type
                meta.gpu = node.config_resource.gpu_num
            res.nodes.append(meta)
        return res

    def _get_training_status(self):
        res = grpc.TrainingStatus()
        if self._task_manager.training_started():
            res.status = TrainingLoopStatus.START
        else:
            res.status = TrainingLoopStatus.PENDING
        return res

    def _check_fault_node(self):
        rdzv_manager: NetworkCheckRendezvousManager = self._rdzv_managers[
            RendezvousName.NETWORK_CHECK
        ]
        nodes, reason = rdzv_manager.check_fault_node()
        res = grpc.NetworkCheckResult(nodes=nodes, reason=reason)
        return res

    def _check_straggler(self):
        rdzv_manager: NetworkCheckRendezvousManager = self._rdzv_managers[
            RendezvousName.NETWORK_CHECK
        ]
        nodes, reason = rdzv_manager.get_straggler()
        res = grpc.NetworkCheckResult(nodes=nodes, reason=reason)
        return res

    def _join_rendezvous(self, request: grpc.JoinRendezvousRequest):
        rdzv_manager = self._rdzv_managers[request.rdzv_name]
        node_rank = request.node_rank
        if node_rank == -1:  # Back compatibility
            node_rank = request.node_id
        round = rdzv_manager.join_rendezvous(
            request.node_id,
            node_rank,
            request.local_world_size,
            request.node_ip,
        )
        if request.rdzv_name == RendezvousName.NETWORK_CHECK:
            # The waiting node in the training rdzv should clear if
            # a worker join network-check rdzv.
            training_manager = self._rdzv_managers[
                RendezvousName.ELASTIC_TRAINING
            ]
            training_manager.clear_waiting_nodes()
        res = grpc.RendezvousState(round=round)
        return res

    def _num_nodes_waiting(self, rdzv_name):
        waiting_num = self._rdzv_managers[rdzv_name].num_nodes_waiting()
        res = grpc.RendezvousState(waiting_num=waiting_num)
        return res

    def _get_comm_world(self, request: grpc.CommWorldRequest):
        rdzv_manager = self._rdzv_managers[request.rdzv_name]
        rdzv_round, group, nodes = rdzv_manager.get_comm_world(request.node_id)
        res = grpc.RendezvousState(world={})
        res.group = group
        res.round = rdzv_round
        for rank, meta in nodes.items():
            res.world[rank] = meta.process_num
        if nodes and request.rdzv_name == RendezvousName.ELASTIC_TRAINING:
            rdzv_round = rdzv_manager.get_rdzv_round()
            metrics = {CustomMetricKeys.RDZV_ROUND: rdzv_round}
            self._job_metric_collector.collect_custom_data(metrics)
        return res

    def _kv_store_get(self, request: grpc.KeyValuePair):
        value = self._kv_store.get(request.key)
        res = grpc.KeyValuePair(request.key, value)
        return res

    def _get_paral_config(self):
        res = self._job_manager.get_opt_strategy()
        if not res:
            res = grpc.ParallelConfig()
        return res

    def _need_to_restart_training(self, node_type, node_id):
        restart = self._job_manager.verify_restarting_worker_training(
            node_type, node_id
        )
        res = grpc.ParallelConfig()
        res.restart = restart
        return res

    def report(self, request, _):
        node_type = request.node_type
        node_id = request.node_id
        message = grpc.deserialize_message(request.data)

        response = elastic_training_pb2.Response()
        if not message:
            return response

        success = False
        if isinstance(message, grpc.DatasetShardParams):
            success = self._collect_dataset_shard_params(message)
        elif isinstance(message, grpc.ResourceStats):
            success = self._update_node_resource_usage(
                node_type, node_id, message
            )
        elif isinstance(message, grpc.ModelInfo):
            success = self._collect_model_info(message)
        elif isinstance(message, grpc.GlobalStep):
            success = self._collect_global_step(message)
        elif isinstance(message, grpc.ShardCheckpoint):
            success = self._restore_shard_checkpoint(message)
        elif isinstance(message, grpc.TaskResult):
            success = self._report_task_result(message)
        elif isinstance(message, grpc.ClusterVersion):
            success = self._update_cluster_version(message)
        elif isinstance(message, grpc.NodeAddress):
            success = self._update_node_address(message)
        elif isinstance(message, grpc.NodeEvent):
            success = self._deal_with_reported_node_event(message)
        elif isinstance(message, grpc.SyncJoin):
            success = self._join_sync(node_type, node_id, message)
        elif isinstance(message, grpc.SyncFinish):
            success = self._sync_finished(message)
        elif isinstance(message, grpc.SyncBarrier):
            success = self._barrier(message)
        elif isinstance(message, grpc.NodeFailure):
            success = self._report_failure(node_type, node_id, message)
        elif isinstance(message, grpc.RendezvousParams):
            success = self._report_rdzv_params(message)
        elif isinstance(message, grpc.PsReady):
            success = self._ready_for_ps_relaunch()
        elif isinstance(message, grpc.KeyValuePair):
            success = self._kv_store_set(message)
        elif isinstance(message, grpc.ParallelConfig):
            success = self._report_paral_config(node_type, node_id, message)
        elif isinstance(message, grpc.NodeCheckpointState):
            success = self._sync_checkpoint(node_type, node_id, message)
        elif isinstance(message, grpc.DiagnosisReportData):
            success = self._report_node_diagnosis_data(message)
        elif isinstance(message, grpc.Event):
            success = self._report_event(message)

        response.success = success
        return response

    def _ready_for_ps_relaunch(self):
        self._job_manager.post_ps_ready()
        return True

    def _collect_dataset_shard_params(self, metrics: grpc.DatasetShardParams):
        num_minibatches_per_task = (
            metrics.num_minibatches_per_shard
            or _DEFAULT_NUM_MINIBATCHES_PER_SHARD
        )
        shard_size = metrics.batch_size * num_minibatches_per_task
        splitter = new_dataset_splitter(
            metrics.shuffle,
            shard_size,
            metrics.dataset_size,
            metrics.num_epochs,
            metrics.dataset_name,
            metrics.storage_type,
        )
        self._task_manager.new_dataset(
            metrics.batch_size,
            metrics.dataset_size,
            metrics.dataset_name,
            splitter,
            metrics.task_type,
        )
        if self._job_metric_collector:
            self._job_metric_collector.collect_dataset_metric(
                metrics.dataset_name,
                metrics.dataset_size,
                metrics.storage_type,
            )
            if metrics.task_type == elastic_training_pb2.TRAINING:
                self._job_metric_collector.collect_training_hyper_params(
                    metrics.num_epochs, metrics.batch_size
                )
        return True

    def _update_node_resource_usage(
        self, node_type, node_id, metrics: grpc.ResourceStats
    ):
        logger.debug(
            f"Update resource usage for {node_type}-{node_id},"
            f"cpu={metrics.cpu}, memory={metrics.memory},"
            f"gpu_stats={metrics.gpu_stats}"
        )
        if self._job_manager:
            self._job_manager.update_node_resource_usage(
                node_type,
                node_id,
                metrics.cpu,
                metrics.memory,
                metrics.gpu_stats,
            )
        return True

    def _collect_model_info(self, metrics: grpc.ModelInfo):
        if self._job_metric_collector:
            self._job_metric_collector.collect_model_metric(metrics)
        return True

    def _collect_global_step(self, metrics: grpc.GlobalStep):
        self._speed_monitor.collect_global_step(
            metrics.step, metrics.timestamp
        )
        self._collect_runtime_stats()
        self._check_start_auto_scale_worker()
        return True

    def _restore_shard_checkpoint(self, message: grpc.ShardCheckpoint):
        success = self._task_manager.restore_dataset_from_checkpoint(
            message.content
        )
        return success

    def _collect_runtime_stats(self):
        if self._job_metric_collector and self._job_manager:
            nodes = self._job_manager.get_running_nodes()
            self._job_metric_collector.collect_runtime_stats(
                self._speed_monitor, nodes
            )

    def _report_task_result(self, request: grpc.TaskResult):
        success = True
        if request.err_message:
            logger.warning("Worker reported error: " + request.err_message)
            success = False
        task, _ = self._task_manager.report_dataset_task(request, success)
        if (
            not self._start_autoscale
            and self._job_manager
            and self._speed_monitor.completed_global_step == 0
            and int(time.time()) - self._start_training_time
            > _dlock_context.seconds_to_autoscale_worker
        ):
            logger.info("Start autoscale for non-training jobs")
            self._job_manager.start_auto_scaling()
            self._start_autoscale = True

        if (
            self._job_metric_collector
            and task
            and task.task_type == elastic_training_pb2.PREDICTION
        ):
            self._collect_runtime_stats()
            self._check_start_auto_scale_worker()
        return success

    def _check_start_auto_scale_worker(self):
        sample_count = self._speed_monitor.get_sample_count()
        if (
            not self._start_autoscale
            and sample_count >= _dlock_context.sample_count_to_adjust_worker
        ):
            logger.info(
                "Start autoscale with %s stats samples",
                sample_count,
            )
            self._job_manager.start_auto_scaling()
            self._start_autoscale = True

    def _update_cluster_version(self, message: grpc.ClusterVersion):
        if not self._elastic_ps_service:
            return False

        if message.task_type == NodeType.WORKER:
            self._elastic_ps_service.update_worker_version(
                message.task_id, message.version_type, message.version
            )
        elif message.task_type == NodeType.PS:
            self._elastic_ps_service.update_ps_version(
                message.task_id, message.version_type, message.version
            )
        return True

    def _update_node_address(self, message: grpc.NodeAddress):
        self._job_manager.update_node_service_addr(
            node_type=message.type,
            node_id=message.id,
            service_addr=message.addr,
        )
        return True

    def _deal_with_reported_node_event(self, message: grpc.NodeEvent):
        node = Node(
            node_type=message.node.type,
            node_id=message.node.id,
            rank_index=message.node.rank,
        )
        event = NodeEvent(message.event_type, node)

        # let rdzv manager deal with rendezvous issue
        if event.is_node_check_event():
            net_rdzv_manager = self._rdzv_managers.get(
                RendezvousName.NETWORK_CHECK, None
            )
            if net_rdzv_manager:
                succeed = (
                    event.event_type == NodeEventType.NODE_CHECK_SUCCEEDED
                )
                net_rdzv_manager.report_network_check_result(
                    node.rank_index, succeed, message.event_elapsed_time
                )

        # let job manager deal with node issue
        self._job_manager.process_reported_node_event(event)
        return True

    def _join_sync(self, node_type, node_id, message: grpc.SyncJoin):
        success = False
        if self._sync_service:
            success = self._sync_service.join_sync(
                message.sync_name, node_type, node_id
            )
        return success

    def _sync_finished(self, message: grpc.SyncFinish):
        success = False
        if self._sync_service:
            success = self._sync_service.sync_finished(message.sync_name)
        return success

    def _barrier(self, message: grpc.SyncBarrier):
        if not self._sync_service:
            return False
        if message.notify:
            success = self._sync_service.notify_barrier(message.barrier_name)
        else:
            success = self._sync_service.barrier(message.barrier_name)
        return success

    def _report_rdzv_params(self, message: grpc.RendezvousParams):
        # Enable auto-scaling workers if elasticity is enabled.
        for manager in self._rdzv_managers.values():
            manager.update_rdzv_params(
                min_nodes=message.min_nodes,
                max_nodes=message.max_nodes,
                waiting_timeout=message.waiting_timeout,
                node_unit=message.node_unit,
            )

        join_timeout = message.join_timeout
        if join_timeout == 0:  # Back compatibility
            join_timeout = JobConstant.RDZV_JOIN_TIMEOUT_DEFAULT
        self._job_manager.update_node_required_info(
            message.min_nodes, message.max_nodes, join_timeout
        )
        return True

    def _report_failure(self, node_type, node_id, message: grpc.NodeFailure):
        self._job_manager.handle_training_failure(
            node_type,
            node_id,
            message.restart_count,
            message.error_data,
            message.level,
        )
        if message.level == TrainingExceptionLevel.RDZV_ERROR:
            custom_data = {
                CustomMetricKeys.TRAINING_ERROR_LEVEL: message.level,
                CustomMetricKeys.ERROR_CONTENT: message.error_data,
            }
            self._job_metric_collector.collect_custom_data(custom_data)
        return True

    def _kv_store_set(self, message: grpc.KeyValuePair):
        self._kv_store.set(message.key, message.value)
        return True

    def _report_paral_config(
        self, node_type, node_id, message: grpc.ParallelConfig
    ):
        if self._job_manager:
            logger.debug(
                "Update parallel config for %s-%s: %s",
                node_type,
                node_id,
                message,
            )
            self._job_manager.update_node_paral_config(
                node_type, node_id, message
            )
        return True

    def _sync_checkpoint(
        self, node_type, node_id, message: grpc.NodeCheckpointState
    ):
        if RendezvousName.ELASTIC_TRAINING not in self._rdzv_managers:
            return False
        rdzv_manager = self._rdzv_managers[RendezvousName.ELASTIC_TRAINING]
        return rdzv_manager.sync_ckpt_nodes(node_id, message.step)

    def _report_node_diagnosis_data(self, message: grpc.DiagnosisReportData):
        if self._diagnosis_manager:
            data_cls: Optional[DiagnosisData] = getattr(
                self._diagnosis_data_module,
                message.data_cls,
            )
            if data_cls is None:
                logger.warning(
                    "Invalid diagnosis report "
                    f"data type: {message.data_cls}"
                )
                return False
            data_obj = data_cls.from_json(message.data_content)
            self._diagnosis_manager.collect_diagnosis_data(data_obj)
        return True

    def _sync_training_ports(
        self, node_id, message: grpc.SyncTrainingPort
    ) -> grpc.SyncTrainingPort:
        logger.info(f"try to sync port {message.port} from {node_id}")
        sync_ports: SyncNodeTrainingPorts = (
            self._job_manager.sync_node_training_port(node_id, message.port)
        )
        return grpc.SyncTrainingPort(
            port=sync_ports.training_port, newport=sync_ports.next_check_port
        )

    def _report_event(self, message: grpc.Event):
        if self._error_monitor:
            self._error_monitor.report_event(
                message.event_type,
                message.instance,
                message.action,
                message.msg,
                message.labels,
            )
        return True

    def _report_heartbeat(
        self, node_type, node_id, message: grpc.HeartBeat
    ) -> grpc.HeartbeatResponse:
        action = self._job_manager.collect_node_heart_beat(
            node_type, node_id, message.timestamp
        )
        grpc_action = grpc.DiagnosisAction(
            action.__class__.__name__,
            action.to_json(),
        )
        return grpc.HeartbeatResponse(action=grpc_action)


def create_master_service(
    port,
    task_manager,
    job_manager,
    speed_monitor,
    rdzv_managers,
    diagnosis_manager,
    job_metric_collector,
    elastic_ps_service,
    sync_service,
    error_monitor=None,
) -> MasterServicer:
    """Create GRPC server"""
    logger.info("Creating master service")
    server = grpc_lib.server(
        futures.ThreadPoolExecutor(max_workers=64),
        options=[
            ("grpc.max_send_message_length", GRPC.MAX_SEND_MESSAGE_LENGTH),
            (
                "grpc.max_receive_message_length",
                GRPC.MAX_RECEIVE_MESSAGE_LENGTH,
            ),
        ],
    )
    master_servicer = MasterServicer(
        task_manager=task_manager,
        job_manager=job_manager,
        speed_monitor=speed_monitor,
        rdzv_managers=rdzv_managers,
        diagnosis_manager=diagnosis_manager,
        job_metric_collector=job_metric_collector,
        elastic_ps_service=elastic_ps_service,
        sync_service=sync_service,
        error_monitor=error_monitor,
    )

    elastic_training_pb2_grpc.add_MasterServicer_to_server(
        master_servicer, server
    )
    server.add_insecure_port("[::]:{}".format(port))
    logger.info("The port of the master server is: %d", port)

    return server
