# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

# pylint: disable=line-too-long
from azure.cli.core.commands import CliCommandType


def load_command_table(self, _):

    with self.command_group('cloudshell') as g:
        g.custom_command('connect', 'connect_cloudshell', is_preview=True)
