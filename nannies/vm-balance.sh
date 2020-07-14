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

echo -n "INFO: vm balancing (dry-run only for now) - "
date
if [ "$VM_BALANCE_DRY_RUN" = "False" ] || [ "$VM_BALANCE_DRY_RUN" = "false" ]; then
    DRY_RUN=""
else
    # DRY_RUN="--dry-run"
    DRY_RUN=""
fi

python3 /scripts/vm_load_balance.py $DRY_RUN --vc_host $VM_BALANCE_VCHOST --vc_user $VM_BALANCE_VCUSER --vc_password $VM_BALANCE_VCPASSWORD --region $REGION --username $OS_USERNAME --password $OS_PASSWORD --user_domain_name $OS_USER_DOMAIN_NAME --project_name $OS_PROJECT_NAME --project_domain_name $OS_PROJECT_DOMAIN_NAME --interval $VM_BALANCE_INTERVAL



