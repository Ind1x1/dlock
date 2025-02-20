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

import os

from dlock.python.common.constants import (
    DistributionStrategy,
    NodeType,
    PlatformType,
)
from dlock.python.common.global_context import Context
from dlock.python.common.log import default_logger as logger
from dlock.python.master.args import parse_master_args
from dlock.python.scheduler.factory import new_job_args
from dlock.python.scheduler.job import JobArgs

_dlock_context = Context.singleton_instance()


def update_context(job_args: JobArgs):
    for node_type, node_args in job_args.node_args.items():
        if node_type == NodeType.WORKER:
            _dlock_context.auto_worker_enabled = node_args.auto_scale
        elif node_type == NodeType.PS:
            _dlock_context.auto_ps_enabled = node_args.auto_scale
    _dlock_context.relaunch_always = job_args.relaunch_always
    if job_args.distribution_strategy == DistributionStrategy.ALLREDUCE:
        _dlock_context.relaunch_always = True
    _dlock_context.set_params_from_brain()
    _dlock_context.print_config()


def run(args):
    job_args = new_job_args(args.platform, args.job_name, args.namespace)
    job_args.initilize()
    logger.info("Job args : %s", job_args.to_json(indent=4))
    _dlock_context.config_master_port(port=args.port)
    if job_args.platform == PlatformType.LOCAL:
        from dlock.python.master.local_master import LocalJobMaster

        worker = job_args.node_args[NodeType.WORKER].group_resource
        worker.count = args.node_num
        master = LocalJobMaster(args.port, job_args)
    else:
        from dlock.python.master.dist_master import DistributedJobMaster

        update_context(job_args)
        master = DistributedJobMaster(_dlock_context.master_port, job_args)
    master.prepare()
    return master.run()


def main():
    args = parse_master_args()
    exit_code = run(args)
    return exit_code


if __name__ == "__main__":
    os._exit(main())
