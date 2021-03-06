# -*- coding: utf-8 -*-
#
# Copyright 2016 Rackspace US, Inc.
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
"""Module for testing snapshot module."""

import datetime
import dateutil
import boto3
from moto import mock_ec2, mock_sns, mock_dynamodb2, mock_sts, mock_iam
from moto import mock_events, mock_cloudformation
from ebs_snapper import snapshot, dynamo, utils, mocks
from ebs_snapper import AWS_MOCK_ACCOUNT
from crontab import CronTab


def setup_module(module):
    import logging
    logging.getLogger('botocore').setLevel(logging.WARNING)
    logging.getLogger('boto3').setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO)


@mock_ec2
@mock_dynamodb2
@mock_sns
@mock_iam
@mock_sts
@mock_events
@mock_cloudformation
def test_perform_fanout_all_regions_snapshot(mocker):
    """Test for method of the same name."""

    # make a dummy SNS topic
    mocks.create_event_rule('ScheduledRuleReplicationFunction')
    mocks.create_sns_topic('CreateSnapshotTopic')
    expected_sns_topic = utils.get_topic_arn('CreateSnapshotTopic', 'us-east-1')

    dummy_regions = ['us-west-2', 'us-east-1']

    # make some dummy instances in two regions
    instance_maps = {}
    for dummy_region in dummy_regions:
        client = boto3.client('ec2', region_name=dummy_region)
        create_results = client.run_instances(ImageId='ami-123abc', MinCount=5, MaxCount=5)
        for instance_data in create_results['Instances']:
            instance_maps[instance_data['InstanceId']] = dummy_region

    # need to filter instances, so need dynamodb present
    mocks.create_dynamodb('us-east-1')
    config_data = {
        "match": {
            "instance-id": instance_maps.keys()
        },
        "snapshot": {
            "retention": "3 days",
            "minimum": 4,
            "frequency": "11 hours"
        }
    }
    dynamo.store_configuration('us-east-1', 'some_unique_id', AWS_MOCK_ACCOUNT, config_data)

    # patch the final message sender method
    ctx = utils.MockContext()
    mocker.patch('ebs_snapper.snapshot.send_fanout_message')
    snapshot.perform_fanout_all_regions(ctx)

    # fan out, and be sure we touched every instance we created before
    for r in dummy_regions:
        snapshot.send_fanout_message.assert_any_call(
            context=ctx,
            region=r,
            sns_topic=expected_sns_topic,
            cli=False)  # pylint: disable=E1103


@mock_ec2
@mock_dynamodb2
@mock_sns
@mock_iam
@mock_sts
def test_perform_snapshot(mocker):
    """Test for method of the same name."""
    # some default settings for this test
    region = 'us-west-2'

    snapshot_settings = {
        'snapshot': {'minimum': 5, 'frequency': '2 hours', 'retention': '5 days'},
        'match': {'tag:backup': 'yes'}
    }

    # create an instance and record the id
    instance_id = mocks.create_instances(region, count=1)[0]

    # need to filter instances, so need dynamodb present
    mocks.create_dynamodb('us-east-1')
    dynamo.store_configuration('us-east-1', 'some_unique_id', AWS_MOCK_ACCOUNT, snapshot_settings)

    # figure out the EBS volume that came with our instance
    instance_details = utils.get_instance(instance_id, region)
    block_devices = instance_details.get('BlockDeviceMappings', [])
    volume_id = block_devices[0]['Ebs']['VolumeId']

    # determine what we should be tagging the snapshot
    ret, freq = utils.parse_snapshot_settings(snapshot_settings)  # pylint: disable=unused-variable
    now = datetime.datetime.now(dateutil.tz.tzutc())
    delete_on_dt = now + ret
    delete_on = delete_on_dt.strftime('%Y-%m-%d')

    # apply some tags
    client = boto3.client('ec2', region_name=region)
    instance_tags = [
        {'Key': 'Name', 'Value': 'Foo'},
        {'Key': 'Service', 'Value': 'Bar'},
        {'Key': 'backup', 'Value': 'yes'},
    ]
    client.create_tags(DryRun=False, Resources=[instance_id], Tags=instance_tags)

    # override one of the tags
    volume_tags = [{'Key': 'Service', 'Value': 'Baz'}]
    client.create_tags(DryRun=False, Resources=[volume_id], Tags=volume_tags)

    # when combined, we expect tags to be this.
    tags = [
        {'Key': 'Name', 'Value': 'Foo'},
        {'Key': 'Service', 'Value': 'Baz'},
        {'Key': 'backup', 'Value': 'yes'},
    ]

    # patch the final method that takes a snapshot
    mocker.patch('ebs_snapper.utils.snapshot_and_tag')

    # since there are no snapshots, we should expect this to trigger one
    ctx = utils.MockContext()
    snapshot.perform_snapshot(ctx, region)

    # test results
    utils.snapshot_and_tag.assert_any_call(  # pylint: disable=E1103
        instance_id,
        'ami-123abc',
        volume_id,
        delete_on,
        region,
        additional_tags=tags)


@mock_ec2
@mock_dynamodb2
@mock_sns
@mock_iam
@mock_sts
def test_perform_snapshot_skipped(mocker):
    """Test for method of the same name."""
    # some default settings for this test
    region = 'us-west-2'
    snapshot_settings = {
        'snapshot': {'minimum': 5, 'frequency': '2 hours', 'retention': '5 days'},
        'match': {'tag:backup': 'yes'}
    }
    mocks.create_dynamodb('us-east-1')
    dynamo.store_configuration('us-east-1', 'some_unique_id', AWS_MOCK_ACCOUNT, snapshot_settings)

    # create an instance and record the id
    instance_id = mocks.create_instances(region, count=1)[0]

    # figure out the EBS volume that came with our instance
    instance_details = utils.get_instance(instance_id, region)
    block_devices = instance_details.get('BlockDeviceMappings', [])
    volume_id = block_devices[0]['Ebs']['VolumeId']

    # determine what we should be tagging the snapshot
    ret, freq = utils.parse_snapshot_settings(snapshot_settings)  # pylint: disable=unused-variable
    now = datetime.datetime.now(dateutil.tz.tzutc())
    delete_on_dt = now + ret
    delete_on = delete_on_dt.strftime('%Y-%m-%d')

    # now take a snapshot, so we expect this next one to be skipped
    utils.snapshot_and_tag(instance_id, 'ami-123abc', volume_id, delete_on, region)

    # patch the final method that takes a snapshot
    mocker.patch('ebs_snapper.utils.snapshot_and_tag')

    # since there are no snapshots, we should expect this to trigger one
    ctx = utils.MockContext()
    snapshot.perform_snapshot(ctx, region)

    # test results (should not create a second snapshot)
    utils.snapshot_and_tag.assert_not_called()  # pylint: disable=E1103


def test_should_perform_snapshot():
    """Test for method of the same name."""
    # should_perform_snapshot(frequency, now, volume_id, recent=None):

    # good to have 'now'
    now = datetime.datetime.now(dateutil.tz.tzutc())

    # if recent is None, we should always snap
    assert snapshot.should_perform_snapshot('0 1 ? * SUN', now, 'blah')

    # don't snap too soon, an hour after the expected one
    assert snapshot.should_perform_snapshot(
        CronTab('0 1 ? * SUN'),  # sunday at 01:00 UTC
        datetime.datetime(2016, 7, 24, 02, 05),  # 2016-07-24 at 2:05 UTC
        'volume-foo',
        recent=datetime.datetime(2016, 7, 24, 01, 05)  # 2016-07-24 at 1:05 UTC
    ) is False

    # it's been a week! snap it!
    assert snapshot.should_perform_snapshot(
        CronTab('0 1 ? * SUN'),  # sunday at 01:00 UTC
        datetime.datetime(2016, 7, 24, 02, 05),  # 2016-07-24 at 2:05 UTC
        'volume-foo',
        recent=datetime.datetime(2016, 7, 17, 01, 05)  # 2016-07-17 at 1:05 UTC
    )

    # it's been forever, and we snap it hourly!
    assert snapshot.should_perform_snapshot(
        CronTab('@hourly'),  # sunday at 01:00 UTC
        datetime.datetime(2016, 7, 24, 02, 05),  # 2016-07-24 at 2:05 UTC
        'volume-foo',
        recent=datetime.datetime(2016, 7, 17, 01, 05)  # 2016-07-17 at 1:05 UTC
    )

    # it's been ten minutes, and we only snap it hourly -- stop it!
    assert snapshot.should_perform_snapshot(
        CronTab('@hourly'),  # sunday at 01:00 UTC
        datetime.datetime(2016, 7, 24, 02, 05),  # 2016-07-24 at 2:05 UTC
        'volume-foo',
        recent=datetime.datetime(2016, 7, 24, 02, 35)  # 2016-07-24 at 2:35 UTC
    ) is False


@mock_ec2
@mock_dynamodb2
@mock_sns
@mock_iam
@mock_sts
def test_perform_snapshot_ignore_instance(mocker):
    """Test for method of the same name."""
    # some default settings for this test
    region = 'us-west-2'

    snapshot_settings = {
        'snapshot': {'minimum': 5, 'frequency': '2 hours', 'retention': '5 days'},
        'match': {'tag:backup': 'yes'},
        'ignore': []
    }

    # create an instance and record the id
    instance_id = mocks.create_instances(region, count=1)[0]
    snapshot_settings['ignore'].append(instance_id)

    # need to filter instances, so need dynamodb present
    mocks.create_dynamodb('us-east-1')
    dynamo.store_configuration('us-east-1', 'some_unique_id', AWS_MOCK_ACCOUNT, snapshot_settings)

    # patch the final method that takes a snapshot
    mocker.patch('ebs_snapper.utils.snapshot_and_tag')

    # since there are no snapshots, we should expect this to trigger one
    ctx = utils.MockContext()
    snapshot.perform_snapshot(ctx, region)

    # test results
    utils.snapshot_and_tag.assert_not_called()  # pylint: disable=E1103


@mock_ec2
@mock_dynamodb2
@mock_sns
@mock_iam
@mock_sts
def test_perform_snapshot_ignore_volume(mocker):
    """Test for method of the same name."""
    # some default settings for this test
    region = 'us-west-2'

    snapshot_settings = {
        'snapshot': {'minimum': 5, 'frequency': '2 hours', 'retention': '5 days'},
        'match': {'tag:backup': 'yes'},
        'ignore': []
    }

    # create an instance and record the id
    instance_id = mocks.create_instances(region, count=1)[0]

    # need to filter instances, so need dynamodb present
    mocks.create_dynamodb('us-east-1')
    dynamo.store_configuration('us-east-1', 'some_unique_id', AWS_MOCK_ACCOUNT, snapshot_settings)

    # figure out the EBS volume that came with our instance
    instance_details = utils.get_instance(instance_id, region)
    block_devices = instance_details.get('BlockDeviceMappings', [])
    volume_id = block_devices[0]['Ebs']['VolumeId']
    snapshot_settings['ignore'].append(volume_id)

    # patch the final method that takes a snapshot
    mocker.patch('ebs_snapper.utils.snapshot_and_tag')

    # since there are no snapshots, we should expect this to trigger one
    ctx = utils.MockContext()
    snapshot.perform_snapshot(ctx, region)

    # test results
    utils.snapshot_and_tag.assert_not_called()  # pylint: disable=E1103
