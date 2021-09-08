#!/bin/bash
#
# Copyright (c) 2018 SAP SE
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

echo "INFO: cleaning up cinder entities without a valid project in the cinder db"

export OS_USER_DOMAIN_NAME
export OS_PROJECT_NAME
export OS_PASSWORD
export OS_AUTH_URL
export OS_USERNAME
export OS_PROJECT_DOMAIN_NAME

# this is to make click in python3 happy
export LC_ALL=C.UTF-8
export LANG=C.UTF-8

if [ "$CINDER_DB_CLEANUP_DRY_RUN" = "False" ] || [ "$CINDER_DB_CLEANUP_DRY_RUN" = "false" ]; then
    DRY_RUN=""
else
    DRY_RUN="--dry-run"
fi
/var/lib/openstack/bin/python /scripts/db-cleanup.py $DRY_RUN --iterations $CINDER_DB_CLEANUP_ITERATIONS --interval $CINDER_DB_CLEANUP_INTERVAL --cinder
