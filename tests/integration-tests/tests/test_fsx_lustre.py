# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file.
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied.
# See the License for the specific language governing permissions and limitations under the License.
import logging
import re

import boto3
import pytest
from retrying import retry

from assertpy import assert_that
from remote_command_executor import RemoteCommandExecutor
from time_utils import minutes, seconds


@pytest.mark.regions(["us-east-1", "eu-west-1"])
@pytest.mark.instances(["c5.xlarge"])
@pytest.mark.oss(["centos7"])
@pytest.mark.schedulers(["sge"])
@pytest.mark.usefixtures("os", "instance", "scheduler")
def test_fsx_lustre(region, pcluster_config_reader, clusters_factory, s3_bucket_factory, test_datadir):
    """
    Test all FSx Lustre related features.

    Grouped all tests in a single function so that cluster can be reused for all of them.
    """
    mount_dir = "/fsx_mount_dir"
    bucket_name = s3_bucket_factory()
    bucket = boto3.resource("s3", region_name=region).Bucket(bucket_name)
    bucket.upload_file(str(test_datadir / "s3_test_file"), "s3_test_file")
    cluster_config = pcluster_config_reader(bucket_name=bucket_name, mount_dir=mount_dir)
    cluster = clusters_factory(cluster_config)
    remote_command_executor = RemoteCommandExecutor(cluster)

    _test_fsx_lustre_correctly_mounted(remote_command_executor, mount_dir)
    _test_import_path(remote_command_executor, mount_dir)
    _test_fsx_lustre_correctly_shared(remote_command_executor, mount_dir)
    _test_export_path(remote_command_executor, mount_dir, bucket_name)


def _test_fsx_lustre_correctly_mounted(remote_command_executor, mount_dir):
    logging.info("Testing fsx lustre is correctly mounted")
    result = remote_command_executor.run_remote_command("df -h -t lustre --output=source,size,target | tail -n +2")
    assert_that(result.stdout).matches(r"[0-9\.]+@tcp:/fsx\s+3\.4T\s+{mount_dir}".format(mount_dir=mount_dir))

    result = remote_command_executor.run_remote_command("cat /etc/fstab")
    assert_that(result.stdout).matches(
        r"fs-[0-9a-z]+\.fsx\.[a-z1-9\-]+\.amazonaws\.com@tcp:/fsx {mount_dir} lustre defaults,_netdev 0 0".format(
            mount_dir=mount_dir
        )
    )


def _test_import_path(remote_command_executor, mount_dir):
    logging.info("Testing fsx lustre import path")
    result = remote_command_executor.run_remote_command("cat {mount_dir}/s3_test_file".format(mount_dir=mount_dir))
    assert_that(result.stdout).is_equal_to("Downloaded by FSx Lustre")


def _test_fsx_lustre_correctly_shared(remote_command_executor, mount_dir):
    logging.info("Testing fsx lustre correctly mounted on compute nodes")
    remote_command_executor.run_remote_command("touch {mount_dir}/test_file".format(mount_dir=mount_dir))
    job_command = (
        "cat {mount_dir}/s3_test_file "
        "&& cat {mount_dir}/test_file "
        "&& touch {mount_dir}/compute_output".format(mount_dir=mount_dir)
    )
    result = remote_command_executor.run_remote_command("echo '{0}' | qsub".format(job_command))
    job_id = _assert_job_submitted(result.stdout)
    _wait_job_completed(remote_command_executor, job_id)
    status = _get_job_exit_status(remote_command_executor, job_id)
    assert_that(status).is_equal_to("0")
    remote_command_executor.run_remote_command("cat {mount_dir}/compute_output".format(mount_dir=mount_dir))


def _test_export_path(remote_command_executor, mount_dir, bucket_name):
    logging.info("Testing fsx lustre export path")
    remote_command_executor.run_remote_command(
        "echo 'Exported by FSx Lustre' > {mount_dir}/file_to_export".format(mount_dir=mount_dir)
    )
    remote_command_executor.run_remote_command(
        "sudo lfs hsm_archive {mount_dir}/file_to_export && sleep 5".format(mount_dir=mount_dir)
    )
    remote_command_executor.run_remote_command(
        "aws s3 cp s3://{bucket_name}/export_dir/file_to_export ./file_to_export".format(bucket_name=bucket_name)
    )
    result = remote_command_executor.run_remote_command("cat ./file_to_export")
    assert_that(result.stdout).is_equal_to("Exported by FSx Lustre")


def _assert_job_submitted(qsub_output):
    __tracebackhide__ = True
    match = re.search(r"Your job ([0-9]+) \(.+\) has been submitted", qsub_output)
    assert_that(match).is_not_none()
    return match.group(1)


@retry(retry_on_result=lambda result: result != 0, wait_fixed=seconds(7), stop_max_delay=minutes(5))
def _wait_job_completed(remote_command_executor, job_id):
    result = remote_command_executor.run_remote_command("qacct -j {0}".format(job_id), raise_on_error=False)
    return result.return_code


def _get_job_exit_status(remote_command_executor, job_id):
    result = remote_command_executor.run_remote_command("qacct -j {0}".format(job_id))
    match = re.search(r"exit_status\s+([0-9]+)", result.stdout)
    assert_that(match).is_not_none()
    return match.group(1)
