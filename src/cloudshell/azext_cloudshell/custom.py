# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

from knack.util import CLIError


def create_cloudshell(cmd, resource_group_name, cloudshell_name, location=None, tags=None):
    raise CLIError('TODO: Implement `cloudshell create`')


def list_cloudshell(cmd, resource_group_name=None):
    raise CLIError('TODO: Implement `cloudshell list`')


def update_cloudshell(cmd, instance, tags=None):
    with cmd.update_context(instance) as c:
        c.set_param('tags', tags)
    return instance