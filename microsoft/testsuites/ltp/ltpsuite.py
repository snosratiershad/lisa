# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.


from typing import Any, Dict

from assertpy import assert_that

from lisa import (
    TestCaseMetadata,
    TestSuite,
    TestSuiteMetadata,
    notifier,
    schema,
    search_space,
)
from lisa.environment import Environment
from lisa.messages import CommunityTestMessage, TestStatus
from lisa.node import Node
from lisa.testsuite import simple_requirement
from microsoft.testsuites.ltp.ltp import Ltp


@TestSuiteMetadata(
    area="ltp",
    category="community",
    description="""
    This test suite is used to run Ltp related tests.
    """,
)
class LtpTestsuite(TestSuite):
    _TIME_OUT = 12000
    LTP_LITE_TESTS = ["math", "fsx", "ipc", "mm", "sched", "pty", "fs"]

    @TestCaseMetadata(
        description="""
        This test case will run Ltp lite tests.
        """,
        priority=3,
        timeout=_TIME_OUT,
        requirement=simple_requirement(
            min_core_count=8,
            disk=schema.DiskOptionSettings(
                data_disk_count=search_space.IntRange(min=2),
                data_disk_size=search_space.IntRange(min=12),
            ),
        ),
    )
    def ltp_lite(
        self, node: Node, environment: Environment, variables: Dict[str, Any]
    ) -> None:
        # parse variables
        tests = variables.get("ltp_test", "")
        skip_tests = variables.get("ltp_skip_test", "")

        # get comma seperated list of tests
        if tests:
            test_list = tests.split(",")
        else:
            test_list = self.LTP_LITE_TESTS

        # get comma seperated list of tests to skip
        if skip_tests:
            skip_test_list = skip_tests.split(",")
        else:
            skip_test_list = []

        # run ltp lite tests
        drive_name = "/dev/sdc"
        results = node.tools[Ltp].run_test(
            "ltp-lite", environment, test_list, skip_test_list, drive_name=drive_name
        )

        # assert that all tests passed
        failed_tests = []
        for result in results:
            if result.status == TestStatus.FAILED:
                failed_tests.append(result.name)

            # create test result message
            community_message = CommunityTestMessage()
            community_message.name = result.name
            community_message.suite_name = self.__class__.__name__
            community_message.status = result.status
            community_message.information["architecture"] = result.architecture
            community_message.information["version"] = result.version
            community_message.information["exit_value"] = str(result.exit_value)

            # notify community test result
            notifier.notify(community_message)

        assert_that(
            failed_tests, f"The following tests failed: {failed_tests}"
        ).is_empty()
