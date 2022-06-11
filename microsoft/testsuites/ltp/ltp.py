# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import List, Optional, Type

from assertpy import assert_that

from lisa.base_tools.cat import Cat
from lisa.environment import Environment
from lisa.executable import Tool
from lisa.messages import TestStatus
from lisa.operating_system import CBLMariner, Debian, Fedora, Posix, Redhat, Suse
from lisa.tools import (
    Echo,
    Free,
    Gcc,
    Git,
    Ls,
    Make,
    Mkdir,
    MkSwap,
    Nproc,
    Rm,
    SwapOn,
    Sysctl,
)
from lisa.tools.chmod import Chmod
from lisa.util import LisaException, find_patterns_in_lines


@dataclass
class LtpResult:
    version: str = ""
    architecture: str = ""
    name: str = ""
    status: TestStatus = TestStatus.QUEUED
    exit_value: int = 0


class Ltp(Tool):
    # Test Start Time: Wed Jun  8 23:43:08 2022
    _RESULT_TIMESTAMP_REGEX = re.compile(r"Test Start Time: (.*)\s+")

    # abs01  PASS  0
    _RESULT_TESTCASE_REGEX = re.compile(r"(.*)\s+(PASS|CONF|FAIL)\s+(\d+)")

    # Machine Architecture: x86_64
    _RESULT_LTP_ARCH_REGEX = re.compile(r"Machine Architecture: (.*)\s+")

    LTP_DIR_NAME = "ltp"
    LTP_TESTS_GIT_TAG = "20190930"
    LTP_GIT_URL = "https://github.com/linux-test-project/ltp.git"
    BUILD_REQUIRED_DISK_SIZE_IN_GB = 2
    LTP_RESULT_PATH = "/opt/ltp/ltp-results.log"
    LTP_OUTPUT_PATH = "/opt/ltp/ltp-output.log"
    LTP_SKIP_FILE = "/opt/ltp/skipfile"

    @property
    def command(self) -> str:
        return "/opt/ltp/runltp"

    @property
    def dependencies(self) -> List[Type[Tool]]:
        return [Make, Gcc, Git]

    @property
    def can_install(self) -> bool:
        return True

    def run_test(
        self,
        lisa_test_name: str,
        environment: Environment,
        ltp_tests: List[str],
        skip_tests: List[str],
        drive_name: Optional[str] = None,
    ) -> List[LtpResult]:
        # tests cannot be empty
        assert_that(ltp_tests, "ltp_tests cannot be empty").is_not_empty()
        ls = self.node.tools[Ls]
        rm = self.node.tools[Rm]

        # remove skipfile if it exists
        if ls.path_exists(self.LTP_SKIP_FILE):
            self._log.debug(f"Removing skipfile: {self.LTP_SKIP_FILE}")
            rm.remove_file(self.LTP_SKIP_FILE, sudo=True)

        # remove results file if it exists
        if ls.path_exists(self.LTP_RESULT_PATH, sudo=True):
            self._log.debug(f"Removing {self.LTP_RESULT_PATH}")
            rm.remove_file(self.LTP_RESULT_PATH, sudo=True)

        # remove output file if it exists
        if ls.path_exists(self.LTP_OUTPUT_PATH, sudo=True):
            self._log.debug(f"Removing {self.LTP_OUTPUT_PATH}")
            rm.remove_file(self.LTP_OUTPUT_PATH, sudo=True)

        # add parameters for the test logging
        parameters = f"-p -q -l {self.LTP_RESULT_PATH} -o {self.LTP_OUTPUT_PATH} "

        # add the list of tests to run
        parameters += f"-f {','.join(ltp_tests)} "

        # add logging and output file parameter
        if drive_name:
            parameters = f"-z {drive_name} "

        # add the list of skip tests to run
        if len(skip_tests) > 0:
            # write skip test to skipfile with newline separator
            skip_file_value = "\n".join(skip_tests)
            self.node.tools[Echo].write_to_file(
                skip_file_value, PurePosixPath(self.LTP_SKIP_FILE), sudo=True
            )
            parameters += f"-S {self.LTP_SKIP_FILE} "

        # run ltp tests
        self.run(parameters, sudo=True, force_run=True, timeout=12000)

        # parse results
        result_messages = self._parse_results(lisa_test_name)

        return result_messages

    def _install(self) -> bool:
        assert isinstance(self.node.os, Posix), f"{self.node.os} is not supported"

        # install common dependencies
        self.node.os.install_packages(
            [
                "m4",
                "bison",
                "flex",
                "psmisc",
                "autoconf",
                "automake",
            ]
        )

        # install distro specific dependencies
        if isinstance(self.node.os, Fedora):
            self.node.os.install_packages(
                ["libaio-devel", "libattr", "libcap-devel", "libdb"]
            )
            if not (
                isinstance(self.node.os, Redhat)
                and self.node.os.information.release >= "8.0"
            ):
                self.node.os.install_packages(["db4-utils"])
            else:
                self.node.os.install_packages(["ntp"])
        elif isinstance(self.node.os, Debian):
            self.node.os.install_packages(
                [
                    "ntp",
                    "libaio-dev",
                    "libattr1",
                    "libcap-dev",
                    "keyutils",
                    "libdb4.8",
                    "libberkeleydb-perl",
                    "expect",
                    "dh-autoreconf",
                    "gdb",
                    "libnuma-dev",
                    "quota",
                    "genisoimage",
                    "db-util",
                    "unzip",
                    "exfat-utils",
                ]
            )
        elif isinstance(self.node.os, Suse):
            self.node.os.install_packages(
                [
                    "ntp",
                    "git-core",
                    "db48-utils",
                    "libaio-devel",
                    "libattr1",
                    "libcap-progs",
                    "libdb-4_8",
                    "perl-BerkeleyDB",
                ]
            )
        elif isinstance(self.node.os, CBLMariner):
            self.node.os.install_packages(
                [
                    "kernel-headers",
                    "binutils",
                    "glibc-devel",
                    "zlib-devel",
                ]
            )
        else:
            raise LisaException(f"{self.node.os} is not supported")

        # Some CPU time is assigned to set real-time scheduler and it affects
        # all cgroup test cases. The values for rt_period_us(1000000us or 1s)
        # and rt_runtime_us (950000us or 0.95s). This gives 0.05s to be used
        # by non-RT tasks.
        if self.node.shell.exists(
            PurePosixPath("/sys/fs/cgroup/cpu/user.slice/cpu.rt_runtime_us")
        ):
            runtime_us = self.node.tools[Cat].read(
                "/sys/fs/cgroup/cpu/user.slice/cpu.rt_runtime_us",
                force_run=True,
                sudo=True,
            )
            runtime_us_int = int(runtime_us)
            if runtime_us_int == 0:
                self.node.tools[Echo].write_to_file(
                    "1000000",
                    PurePosixPath("/sys/fs/cgroup/cpu/cpu.rt_period_us"),
                    sudo=True,
                )
                self.node.tools[Echo].write_to_file(
                    "950000",
                    PurePosixPath("/sys/fs/cgroup/cpu/cpu.rt_runtime_us"),
                    sudo=True,
                )
                self.node.tools[Echo].write_to_file(
                    "1000000",
                    PurePosixPath("/sys/fs/cgroup/cpu/user.slice/cpu.rt_period_us"),
                    sudo=True,
                )
                self.node.tools[Echo].write_to_file(
                    "950000",
                    PurePosixPath("/sys/fs/cgroup/cpu/user.slice/cpu.rt_runtime_us"),
                    sudo=True,
                )

        # Minimum 4M swap space is needed by some mmp test
        if self.node.tools[Free].get_swap_size() < 4:
            self.node.execute("dd if=/dev/zero of=/tmp/swap bs=1M count=1024")
            self.node.tools[MkSwap].run("/tmp/swap")
            self.node.tools[SwapOn].run("/tmp/swap")

        # Fix hung_task_timeout_secs and blocked for more than 120 seconds problem
        sysctl = self.node.tools[Sysctl]
        sysctl.write("vm.dirty_ratio", "10")
        sysctl.write("vm.dirty_background_ratio", "5")
        sysctl.run("-p")

        # define regular stable releases in order to avoid unstable builds
        # https://github.com/linux-test-project/ltp/tags
        # 'ltp_version_git_tag' is passed from Test Definition in
        # .\XML\TestCases\CommunityTests.xml. 'ltp_version_git_tag' default
        # value is defined in .\XML\Other\ReplaceableTestParameters.xml. You can
        # run the ltp test with any tag using LISAv2's Custom Parameters feature.
        build_dir = self.node.find_partition_with_freespace(
            self.BUILD_REQUIRED_DISK_SIZE_IN_GB
        )
        top_src_dir = f"{build_dir}/{self.LTP_DIR_NAME}"

        # remove build directory if it exists
        if self.node.tools[Ls].path_exists(top_src_dir, sudo=True):
            self.node.tools[Rm].remove_directory(top_src_dir, sudo=True)

        # setup build directory
        self.node.tools[Mkdir].create_directory(top_src_dir, sudo=True)
        self.node.tools[Chmod].update_folder(top_src_dir, "a+rwX", sudo=True)

        # clone ltp
        git = self.node.tools[Git]
        ltp_path = git.clone(
            self.LTP_GIT_URL, cwd=PurePosixPath(top_src_dir), dir_name=top_src_dir
        )

        # checkout tag
        git.checkout(ref=f"tags/{self.LTP_TESTS_GIT_TAG}", cwd=ltp_path)

        # build ltp in /opt/ltp since this path is used by some
        # tests, e.g, block_dev test
        make = self.node.tools[Make]
        nprocs = self.node.tools[Nproc].get_num_procs()
        self.node.execute("autoreconf -f", cwd=ltp_path, sudo=True)
        make.make("autotools", cwd=ltp_path, sudo=True)
        self.node.execute("./configure --prefix=/opt/ltp", cwd=ltp_path, sudo=True)
        make.make(f"-j {nprocs} all", cwd=ltp_path, sudo=True)
        make.make(f"-j {nprocs} install SKIP_IDCHECK=1", cwd=ltp_path, sudo=True)

        return self._check_exists()

    def _parse_results(
        self,
        result_file: str,
    ) -> List[LtpResult]:
        # load results from result_file
        result_file = self.node.tools[Cat].read(result_file, force_run=True, sudo=True)
        results: List[LtpResult] = []

        matched = find_patterns_in_lines(
            result_file, [self._RESULT_LTP_ARCH_REGEX, self._RESULT_TESTCASE_REGEX]
        )

        # get architecture
        architecture = matched[0][0].strip()

        # get testcase data
        for result in matched[1]:
            results.append(
                LtpResult(
                    version=self.LTP_TESTS_GIT_TAG,
                    architecture=architecture,
                    name=result[0].strip(),
                    status=self._parse_status_to_test_status(result[1].strip()),
                    exit_value=result[2].strip(),
                )
            )

        return results

    def _parse_status_to_test_status(self, status: str) -> TestStatus:
        if status == "PASS":
            return TestStatus.PASSED
        elif status == "FAIL":
            return TestStatus.FAILED
        elif status == "CONF":
            return TestStatus.SKIPPED
        else:
            raise LisaException(f"Unknown status: {status}")
