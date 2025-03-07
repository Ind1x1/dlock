# Copyright 2023 The DLRover Authors. All rights reserved.
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


"""
``dlock-run`` provides a superset of the functionality as ``torchrun``
with the following additional functionalities:

1. Check the network of node to detect the fault node or straggler.

2. `rdzv-endpoint`, `rdzv-backend` and `rdzv-id` are not required for
multi-node multi-worker.

Usage
--------

Run in the worker Pod with GPU of ElasticJob.
++++++++++++++++++++++++++++++

::

    dlock-run
        --auto-config
        YOUR_TRAINING_SCRIPT.py (--arg1 ... train script args...)

auto-config will set the nnodes as the number of nodes in a job,
nproc_per_node as the number of available GPUs. If the number of
nodes >= 4, it will set the network-check as True. If network-check is True,
dlock-run will launch simple tasks on each node to check whether
the node is slow or fault.

Single-node multi-worker
++++++++++++++++++++++++++++++

::

    dlock-run
        --standalone
        --nproc-per-node=$NUM_TRAINERS
        YOUR_TRAINING_SCRIPT.py (--arg1 ... train script args...)

multi-node multi-worker
+++++++++++++++++++++++++++++++++++

::

    dlock-run
        --nnodes=$NUM_NODES
        --nproc-per-node=$NUM_TRAINERS
        --max-restarts=3
        YOUR_TRAINING_SCRIPT.py (--arg1 ... train script args...)

Elastic (``min=1``, ``max=4``, tolerates up to 3 membership
changes or failures)
+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

::

    dlock-run
        --nnodes=1:4
        --nproc-per-node=$NUM_TRAINERS
        --max-restarts=3
        YOUR_TRAINING_SCRIPT.py (--arg1 ... train script args...)

Note on rendezvous backend
------------------------------

For multi-node training you need to specify:

1. ``--network-check``: Bool, whether to check the node network to find the
    fault node or straggler.
2. ``--rdzv-conf``: We can set timeout into rdzv_conf like
    ```--rdzv-conf join_timeout=600,lastcall_timeout=60,pend_timeout=3600`.

For auto-tuning parallelism configuration, you need to specify:

1. ``--auto-tunning``: Whether to auto tune the batch size and learning rate.
"""
import os
import socket
import sys
import telnetlib
import time
import uuid
from datetime import datetime
from typing import Callable, List, Tuple, Union

from torch.distributed.argparse_util import check_env, env
from torch.distributed.elastic.multiprocessing.api import SubprocessHandler
from torch.distributed.elastic.multiprocessing.errors import record
from torch.distributed.launcher.api import launch_agent as torch_launch_agent
from torch.distributed.run import (
    config_from_args,
    get_args_parser,
    parse_min_max_nnodes,
)

import dlock.python.util.common_util as cu
from dlock.python.common import env_utils, grpc
from dlock.python.common.constants import (
    Accelerators,
    NodeEnv,
    NodeErrorMessage,
    TrainingExceptionLevel,
)
from dlock.python.common.log import default_logger as logger
from dlock.python.elastic_agent.master_client import MasterClient
from dlock.python.elastic_agent.torch.training import (
    ElasticLaunchConfig,
    launch_agent,
)
from dlock.trainer.torch.utils import version_less_than_230


def parse_args(args):
    parser = get_args_parser()
    parser.allow_abbrev = False
    parser.add_argument(
        "--precheck",
        type=int,
        action=env,
        default=0,
        choices=[0, 1, 2],
        help="The level to check the node before starting the training task."
        "Default 0 dose not run check task; the value 1 splits nodes into "
        "groups to runs a matmul and allgather task and each group has 2 "
        "nodes; the value 2 will run an allgather task with all nodes to "
        "test the performance.",
    )
    parser.add_argument(
        "--node_unit",
        "--node-unit",
        type=int,
        action=env,
        default=1,
        help="The number unit of nodes to schedule. The scheduled number of "
        "nodes should be a multiple of node_unit.",
    )
    parser.add_argument(
        "--auto_config",
        "--auto-config",
        action=check_env,
        help="Whether to automatically configure the nnodes "
        "and nproc_per_nodes.",
    )
    parser.add_argument(
        "--auto_tunning",
        "--auto-tunning",
        action=check_env,
        help="Whether to auto-tune the parallel configuration.",
    )
    parser.add_argument(
        "--exclude-straggler",
        "--exclude_straggler",
        action=check_env,
        help="Bool, The node will exit if the node is straggler and "
        "the argument is True. The argument only works when network-check "
        "is True.",
    )
    parser.add_argument(
        "--save_at_breakpoint",
        "--save-at-breakpoint",
        action=check_env,
        help="Bool. If True, the agent in the main process will save the "
        "checkpoint in the memory to the storage if the training "
        "process fails.",
    )
    parser.add_argument(
        "--accelerator",
        type=str,
        action=env,
        default=Accelerators.NVIDIA_GPU,
        choices=[Accelerators.NVIDIA_GPU, Accelerators.ASCEND_NPU],
        help="The type of accelerator chip of the machine.",
    )
    parser.add_argument(
        "--training_port",
        "--training-port",
        type=int,
        action=env,
        default=60000,
        help="The start of training port.",
    )
    parser.add_argument(
        "--numa-affinity",
        "--numa_affinity",
        action=check_env,
        help="bool, set workers processes cpu numa affinity or not",
    )

    # deprecated arguments
    parser.add_argument(
        "--network-check",
        "--network_check",
        action=check_env,
        help="Whether to check network before starting training process.",
    )
    parser.add_argument(
        "--comm-perf-test",
        "--comm_perf_test",
        action=check_env,
        help="Whether to test the communication performance.",
    )
    return parser.parse_args(args)


class elastic_launch:
    """
    Launches an torchelastic agent on the container
    that invoked the entrypoint.

        1. Pass the ``entrypoint`` arguments as non ``kwargs``
            (e.g. no named parameters)/
           ``entrypoint`` can be a function or a command.
        2. The return value is a map of each worker's output mapped
           by their respective global rank.

    Usage

    ::

    def worker_fn(foo):
        # ...

    def main():
        # entrypoint is a function.
        outputs = elastic_launch(LaunchConfig, worker_fn)(foo)
        # return rank 0's output
        return outputs[0]

        # entrypoint is a command and ``script.py`` is the python module.
        outputs = elastic_launch(LaunchConfig, "script.py")(args)
        outputs = elastic_launch(LaunchConfig, "python")("script.py")
    """

    def __init__(
        self,
        config: ElasticLaunchConfig,
        entrypoint: Union[Callable, str, None],
        use_dlock_launch: bool,
    ):
        self._config = config
        self._entrypoint = entrypoint
        self._use_dlock_launch = use_dlock_launch

    def __call__(self, *args):
        if self._use_dlock_launch:
            return launch_agent(self._config, self._entrypoint, list(args))
        else:
            return torch_launch_agent(
                self._config, self._entrypoint, list(args)
            )


def _launch_dlock_local_master(master_addr, job_name, node_num):
    """Launch a subprocess to run the dlock master."""
    logger.info(f"Start dlock master with addr {master_addr}")
    if not master_addr:
        host = "127.0.0.1"
        port = grpc.find_free_port()
    else:
        host = master_addr.split(":")[0]
        port = int(master_addr.split(":")[1])
    cmd = os.getenv("PYTHON_EXEC", sys.executable)
    args = (
        "-u",
        "-m",
        "dlock.python.master.main",
        "--port",
        f"{port}",
        "--node_num",
        f"{node_num}",
        "--job_name",
        job_name,
        "--platform",
        "local",
    )
    if version_less_than_230():
        handler = SubprocessHandler(cmd, args, {}, "", "")
    else:
        handler = SubprocessHandler(cmd, args, {}, "", "", 0)

    dlock_master_addr = f"{host}:{port}"
    return handler, dlock_master_addr


def _check_dlock_master_available(addr, timeout=120):
    """Verify that the master grpc servicer is available."""
    if not addr:
        return False
    host = addr.split(":")[0]
    port = int(addr.split(":")[1])
    start_time = time.time()
    while True:
        try:
            telnetlib.Telnet(host=host, port=port, timeout=3)
            logger.info("dlock master has already started.")
            return True
        except (socket.timeout, ConnectionRefusedError):
            time.sleep(1)
        except socket.gaierror as e:
            client = MasterClient.singleton_instance(addr)
            client.report_failures(
                NodeErrorMessage.SOCKET_GAIERROR,
                level=TrainingExceptionLevel.NODE_ERROR,
            )
            raise e

        if time.time() - start_time > timeout:
            return False


def _elastic_config_from_args(
    args,
) -> Tuple[ElasticLaunchConfig, Union[Callable, str], List[str]]:
    config, cmd, cmd_args = config_from_args(args)

    master_config = _elastic_config_from_master(config)
    elastic_config = ElasticLaunchConfig(**config.__dict__)

    # PyTorch >= 2.3.0 remove log_dir in the LaunchConfig.
    if not version_less_than_230():
        elastic_config.log_dir = config.logs_specs.root_log_dir

    elastic_config.precheck = getattr(args, "precheck", False)
    if master_config.precheck:
        logger.info("Enable precheck by master")
        elastic_config.precheck = master_config.precheck

    elastic_config.network_check = getattr(args, "network_check", False)
    if master_config.network_check:
        logger.info("Enable network checking by master")
        elastic_config.network_check = True

    elastic_config.comm_perf_test = getattr(args, "comm_perf_test", False)
    if master_config.comm_perf_test:
        logger.info("Enable comm_perf_test by master")
        elastic_config.comm_perf_test = True

    elastic_config.numa_affinity = getattr(args, "numa_affinity", False)
    if master_config.numa_affinity:
        logger.info("Enable numa affinity by master")
        elastic_config.numa_affinity = True

    elastic_config.auto_tunning = getattr(args, "auto_tunning", False)
    if master_config.auto_tunning:
        logger.info("Enable auto_tunning by master")
        elastic_config.auto_tunning = True

    elastic_config.auto_config = getattr(args, "auto_config", False)
    if master_config.auto_config:
        logger.info("Enable auto_config by master")
        elastic_config.auto_config = True

    elastic_config.accelerator = getattr(
        args, "accelerator", Accelerators.NVIDIA_GPU
    )

    elastic_config.exclude_straggler = getattr(
        args, "exclude_straggler", False
    )
    if master_config.exclude_straggler:
        elastic_config.exclude_straggler = True
    elastic_config.set_node_unit(getattr(args, "node_unit", 1))
    elastic_config.training_port = getattr(args, "training_port", 60000)
    elastic_config.save_at_breakpoint = getattr(
        args, "save_at_breakpoint", False
    )
    if master_config.save_at_breakpoint:
        elastic_config.save_at_breakpoint = True
    elastic_config.auto_configure_params()
    elastic_config.update_precheck_args()
    elastic_config.rdzv_backend = "dlock-master"
    elastic_config.rdzv_endpoint = ""
    join_timeout = elastic_config.rdzv_configs.get("join_timeout", 600)
    elastic_config.rdzv_configs["timeout"] = join_timeout
    return elastic_config, cmd, cmd_args


def _elastic_config_from_master(config) -> ElasticLaunchConfig:
    elastic_config = ElasticLaunchConfig(**config.__dict__)

    _client = MasterClient.singleton_instance()
    try:
        logger.info("try to get elastic run config from master")
        master_configs = _client.get_elastic_run_config()
    except Exception as e:
        logger.error(f"fail to get elastic config from master: {e}")
        master_configs = {}

    elastic_config.network_check = False
    if "network_check" in master_configs:
        elastic_config.network_check = True

    elastic_config.comm_perf_test = False
    if "comm_perf_test" in master_configs:
        elastic_config.comm_perf_test = True

    elastic_config.auto_tunning = False
    if "auto_tunning" in master_configs:
        elastic_config.auto_tunning = True

    elastic_config.auto_config = False
    if "auto_config" in master_configs:
        elastic_config.auto_config = True

    elastic_config.exclude_straggler = False
    if "exclude_straggler" in master_configs:
        elastic_config.exclude_straggler = True

    elastic_config.save_at_breakpoint = False
    if "save_at_breakpoint" in master_configs:
        elastic_config.save_at_breakpoint = True

    elastic_config.numa_affinity = False
    if "numa_affinity" in master_configs:
        elastic_config.numa_affinity = True

    return elastic_config


def _check_to_use_dlock_run(master_addr, max_nodes, timeout=120):
    if _check_dlock_master_available(master_addr, timeout):
        return True
    elif max_nodes == 1:
        logger.info("Use native torchrun to start job on the single node.")
        return False
    elif not master_addr:
        raise ValueError(
            "dlock job master address cannot be empty. "
            f"Please set the env {NodeEnv.DLOCK_MASTER_ADDR} as "
            "the address of node rank 0"
        )
    else:
        raise ValueError(f"{master_addr} is not connected. ")


def run(args):
    logger.info(f"dlock agent started with: {cu.get_dlock_version()}.")
    master_handler = None
    master_addr = os.getenv(NodeEnv.DLOCK_MASTER_ADDR, "")
    node_rank = env_utils.get_node_rank()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job_name = os.getenv(NodeEnv.JOB_NAME, f"standalone_{timestamp}")
    os.environ[NodeEnv.TORCHELASTIC_RUN_ID] = job_name
    dlock_master_ready = grpc.addr_connected(master_addr)
    _, max_nodes = parse_min_max_nnodes(args.nnodes)
    if not dlock_master_ready and node_rank == 0:
        # Only start the dlock master on the rank-0 node.
        master_handler, master_addr = _launch_dlock_local_master(
            master_addr,
            job_name,
            max_nodes,
        )
        logger.info(f"Set the dlock master addr as {master_addr}")
        os.environ[NodeEnv.DLOCK_MASTER_ADDR] = master_addr
    use_dlock_launch = _check_to_use_dlock_run(master_addr, max_nodes)

    if args.standalone and not use_dlock_launch:
        args.rdzv_backend = "c10d"
        args.rdzv_endpoint = "localhost:29400"
        args.rdzv_id = str(uuid.uuid4())
        logger.info(
            f"\n**************************************\n"
            f"Rendezvous info:\n"
            f"--rdzv-backend={args.rdzv_backend} "
            f"--rdzv-endpoint={args.rdzv_endpoint} "
            f"--rdzv-id={args.rdzv_id}\n"
            f"**************************************\n"
        )

    config, cmd, cmd_args = _elastic_config_from_args(args)
    config.run_id = job_name
    config.role = "dlock-trainer"
    try:
        elastic_launch(
            config=config,
            entrypoint=cmd,
            use_dlock_launch=use_dlock_launch,
        )(*cmd_args)
    finally:
        if master_handler:
            master_handler.close()


@record
def main(args=None):
    args = parse_args(args)
    run(args)


if __name__ == "__main__":
    main()
