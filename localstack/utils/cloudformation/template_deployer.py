import re
import os
import json
import yaml
import base64
import logging
import traceback
import moto.cloudformation.utils
from six import iteritems
from six import string_types
from localstack.utils import common
from localstack.utils.aws import aws_stack
from localstack.utils.testutil import create_zip_file
from localstack.services.awslambda.lambda_api import get_handler_file_from_name

ACTION_CREATE = 'create'
ACTION_DELETE = 'delete'
PLACEHOLDER_RESOURCE_NAME = '__resource_name__'

LOG = logging.getLogger(__name__)

# list of resource types that can be updated
UPDATEABLE_RESOURCES = ['Lambda::Function', 'ApiGateway::Method']

# create safe yaml loader that parses date strings as string, not date objects
NoDatesSafeLoader = yaml.SafeLoader
NoDatesSafeLoader.yaml_implicit_resolvers = {
    k: [r for r in v if r[0] != 'tag:yaml.org,2002:timestamp'] for
    k, v in NoDatesSafeLoader.yaml_implicit_resolvers.items()
}


def str_or_none(o):
    return o if o is None else json.dumps(o) if isinstance(o, (dict, list)) else str(o)


def select_attributes(obj, attrs):
    result = {}
    for attr in attrs:
        if obj.get(attr) is not None:
            result[attr] = str_or_none(obj.get(attr))
    return result


def get_bucket_location_config(**kwargs):
    return {'LocationConstraint': aws_stack.get_region()}


def lambda_get_params():
    return lambda params, **kwargs: params


def get_nested_stack_name(params, **kwargs):
    stack_name = kwargs.get('stack_name', 'stack')
    return '%s-%s' % (stack_name, common.short_uid())


def get_lambda_code_param(params, **kwargs):
    code = params.get('Code', {})
    zip_file = code.get('ZipFile')
    if zip_file and not common.is_base64(zip_file):
        tmp_dir = common.new_tmp_dir()
        handler_file = get_handler_file_from_name(params['Handler'], runtime=params['Runtime'])
        tmp_file = os.path.join(tmp_dir, handler_file)
        common.save_file(tmp_file, zip_file)
        zip_file = create_zip_file(tmp_file, get_content=True)
        code['ZipFile'] = common.to_str(base64.b64encode(zip_file))
        code['ZipFile'] = zip_file
        common.rm_rf(tmp_dir)
    return code


def sns_subscription_params(params, **kwargs):
    def attr_val(val):
        return json.dumps(val) if isinstance(val, (dict, list)) else str(val)

    attrs = ['DeliveryPolicy', 'FilterPolicy', 'RawMessageDelivery', 'RedrivePolicy']
    result = dict([(a, attr_val(params[a])) for a in attrs if a in params])
    return result


# maps resource types to functions and parameters for creation
RESOURCE_TO_FUNCTION = {
    'S3::Bucket': {
        'create': {
            'function': 'create_bucket',
            'parameters': {
                'Bucket': ['BucketName', PLACEHOLDER_RESOURCE_NAME],
                'ACL': lambda params, **kwargs: convert_acl_cf_to_s3(params.get('AccessControl', 'PublicRead')),
                'CreateBucketConfiguration': lambda params, **kwargs: get_bucket_location_config()
            }
        },
        'delete': {
            'function': 'delete_bucket',
            'parameters': {
                'Bucket': 'PhysicalResourceId'
            }
        }
    },
    'SQS::Queue': {
        'create': {
            'function': 'create_queue',
            'parameters': {
                'QueueName': ['QueueName', PLACEHOLDER_RESOURCE_NAME],
                'Attributes': lambda params, **kwargs: select_attributes(params,
                    ['DelaySeconds', 'MaximumMessageSize', 'MessageRetentionPeriod',
                     'VisibilityTimeout', 'RedrivePolicy']
                ),
                'tags': 'Tags'
            }
        },
        'delete': {
            'function': 'delete_queue',
            'parameters': {
                'QueueUrl': 'PhysicalResourceId'
            }
        }
    },
    'SNS::Topic': {
        'create': {
            'function': 'create_topic',
            'parameters': {
                'Name': 'TopicName',
                'Tags': 'Tags'
            }
        },
        'delete': {
            'function': 'delete_topic',
            'parameters': {
                'TopicArn': 'PhysicalResourceId'
            }
        }
    },
    'Logs::LogGroup': {
        # TODO implement
    },
    'Lambda::Function': {
        'create': {
            'function': 'create_function',
            'parameters': {
                'FunctionName': 'FunctionName',
                'Runtime': 'Runtime',
                'Role': 'Role',
                'Handler': 'Handler',
                'Code': get_lambda_code_param,
                'Description': 'Description',
                'Environment': 'Environment',
                'Timeout': 'Timeout',
                'MemorySize': 'MemorySize',
                # TODO add missing fields
            },
            'defaults': {
                'Role': 'test_role'
            }
        }
    },
    'Lambda::Version': {
        'create': {
            'function': 'publish_version',
            'parameters': {
                'FunctionName': 'FunctionName',
                'CodeSha256': 'CodeSha256',
                'Description': 'Description'
            }
        }
    },
    'Lambda::Permission': {},
    'Lambda::EventSourceMapping': {
        'create': {
            'function': 'create_event_source_mapping',
            'parameters': {
                'FunctionName': 'FunctionName',
                'EventSourceArn': 'EventSourceArn',
                'StartingPosition': 'StartingPosition',
                'Enabled': 'Enabled',
                'BatchSize': 'BatchSize',
                'StartingPositionTimestamp': 'StartingPositionTimestamp'
            }
        }
    },
    'DynamoDB::Table': {
        'create': {
            'function': 'create_table',
            'parameters': {
                'TableName': 'TableName',
                'AttributeDefinitions': 'AttributeDefinitions',
                'KeySchema': 'KeySchema',
                'ProvisionedThroughput': 'ProvisionedThroughput',
                'LocalSecondaryIndexes': 'LocalSecondaryIndexes',
                'GlobalSecondaryIndexes': 'GlobalSecondaryIndexes',
                'StreamSpecification': lambda params, **kwargs: (
                    common.merge_dicts(params.get('StreamSpecification'), {'StreamEnabled': True}, default=None))
            },
            'defaults': {
                'ProvisionedThroughput': {
                    'ReadCapacityUnits': 5,
                    'WriteCapacityUnits': 5
                }
            }
        }
    },
    'Events::Rule': {
        'create': [{
            'function': 'put_rule',
            'parameters': {
                'Name': PLACEHOLDER_RESOURCE_NAME,
                'ScheduleExpression': 'ScheduleExpression',
                'EventPattern': 'EventPattern',
                'State': 'State',
                'Description': 'Description'
            }
        }, {
            'function': 'put_targets',
            'parameters': {
                'Rule': PLACEHOLDER_RESOURCE_NAME,
                'EventBusName': 'EventBusName',
                'Targets': 'Targets'
            }
        }]
    },
    'IAM::Role': {
        # TODO implement
    },
    'ApiGateway::RestApi': {
        'create': {
            'function': 'create_rest_api',
            'parameters': {
                'name': 'Name',
                'description': 'Description'
            }
        }
    },
    'ApiGateway::Resource': {
        'create': {
            'function': 'create_resource',
            'parameters': {
                'restApiId': 'RestApiId',
                'pathPart': 'PathPart',
                'parentId': 'ParentId'
            }
        }
    },
    'ApiGateway::Method': {
        'create': {
            'function': 'put_method',
            'parameters': {
                'restApiId': 'RestApiId',
                'resourceId': 'ResourceId',
                'httpMethod': 'HttpMethod',
                'authorizationType': 'AuthorizationType',
                'requestParameters': 'RequestParameters'
            }
        }
    },
    'ApiGateway::Method::Integration': {
    },
    'ApiGateway::Deployment': {
        'create': {
            'function': 'create_deployment',
            'parameters': {
                'restApiId': 'RestApiId',
                'stageName': 'StageName',
                'stageDescription': 'StageDescription',
                'description': 'Description'
            }
        }
    },
    'ApiGateway::GatewayResponse': {
        'create': {
            'function': 'put_gateway_response',
            'parameters': {
                'restApiId': 'RestApiId',
                'responseType': 'ResponseType',
                'statusCode': 'StatusCode',
                'responseParameters': 'ResponseParameters',
                'responseTemplates': 'ResponseTemplates'
            }
        }
    },
    'Kinesis::Stream': {
        'create': {
            'function': 'create_stream',
            'parameters': {
                'StreamName': 'Name',
                'ShardCount': 'ShardCount'
            },
            'defaults': {
                'ShardCount': 1
            }
        },
        'delete': {
            'function': 'delete_stream',
            'parameters': {
                'StreamName': 'PhysicalResourceId'
            }
        }
    },
    'StepFunctions::StateMachine': {
        'create': {
            'function': 'create_state_machine',
            'parameters': {
                'name': ['StateMachineName', PLACEHOLDER_RESOURCE_NAME],
                'definition': 'DefinitionString',
                'roleArn': lambda params, **kwargs: get_role_arn(params.get('RoleArn'), **kwargs)
            }
        }
    },
    'StepFunctions::Activity': {
        'create': {
            'function': 'create_activity',
            'parameters': {
                'name': ['Name', PLACEHOLDER_RESOURCE_NAME],
                'tags': 'Tags'
            }
        }
    },
    'SNS::Subscription': {
        'create': {
            'function': 'subscribe',
            'parameters': {
                'TopicArn': 'TopicArn',
                'Protocol': 'Protocol',
                'Endpoint': 'Endpoint',
                'Attributes': sns_subscription_params
            }
        }
    },
    'CloudFormation::Stack': {
        'create': {
            'function': 'create_stack',
            'parameters': {
                'StackName': get_nested_stack_name,
                'TemplateURL': 'TemplateURL'
            }
        }
    }
}


# ----------------
# UTILITY METHODS
# ----------------

def convert_acl_cf_to_s3(acl):
    """ Convert a CloudFormation ACL string (e.g., 'PublicRead') to an S3 ACL string (e.g., 'public-read') """
    return re.sub('(?<!^)(?=[A-Z])', '-', acl).lower()


def retrieve_topic_arn(topic_name):
    topics = aws_stack.connect_to_service('sns').list_topics()['Topics']
    topic_arns = [t['TopicArn'] for t in topics if t['TopicArn'].endswith(':%s' % topic_name)]
    return topic_arns[0]


def get_role_arn(role_arn, **kwargs):
    role_arn = resolve_refs_recursively(kwargs.get('stack_name'), role_arn, kwargs.get('resources'))
    return aws_stack.role_arn(role_arn)


# ---------------------
# CF TEMPLATE HANDLING
# ---------------------

def parse_template(template):
    try:
        return json.loads(template)
    except Exception:
        yaml.add_multi_constructor('', moto.cloudformation.utils.yaml_tag_constructor, Loader=NoDatesSafeLoader)
        try:
            return yaml.safe_load(template)
        except Exception:
            return yaml.load(template, Loader=NoDatesSafeLoader)


def template_to_json(template):
    template = parse_template(template)
    return json.dumps(template)


def get_resource_type(resource):
    res_type = resource.get('ResourceType') or resource.get('Type') or ''
    parts = res_type.split('::', 1)
    if len(parts) == 1:
        return None
    return parts[1]


def get_service_name(resource):
    res_type = resource.get('Type', resource.get('ResourceType', ''))
    parts = res_type.split('::')
    if len(parts) == 1:
        return None
    if res_type.endswith('Cognito::UserPool'):
        return 'cognito-idp'
    if parts[-2] == 'Cognito':
        return 'cognito-idp'
    return parts[1].lower()


def get_resource_name(resource):
    res_type = get_resource_type(resource)
    properties = resource.get('Properties') or {}
    name = properties.get('Name')
    if name:
        return name

    # try to extract name from attributes
    if res_type == 'S3::Bucket':
        name = properties.get('BucketName')
    elif res_type == 'SQS::Queue':
        name = properties.get('QueueName')
    elif res_type == 'Cognito::UserPool':
        name = properties.get('PoolName')
    elif res_type == 'StepFunctions::StateMachine':
        name = properties.get('StateMachineName')
    else:
        LOG.warning('Unable to extract name for resource type "%s"' % res_type)

    return name


def get_client(resource, func_config):
    resource_type = get_resource_type(resource)
    service = get_service_name(resource)
    resource_config = RESOURCE_TO_FUNCTION.get(resource_type)
    if resource_config is None:
        raise Exception('CloudFormation deployment for resource type %s not yet implemented' % resource_type)
    try:
        if func_config.get('boto_client') == 'resource':
            return aws_stack.connect_to_resource(service)
        return aws_stack.connect_to_service(service)
    except Exception as e:
        LOG.warning('Unable to get client for "%s" API, skipping deployment: %s' % (service, e))
        return None


def describe_stack_resource(stack_name, logical_resource_id):
    client = aws_stack.connect_to_service('cloudformation')
    try:
        result = client.describe_stack_resource(StackName=stack_name, LogicalResourceId=logical_resource_id)
        return result['StackResourceDetail']
    except Exception as e:
        LOG.warning('Unable to get details for resource "%s" in CloudFormation stack "%s": %s' %
                    (logical_resource_id, stack_name, e))


def retrieve_resource_details(resource_id, resource_status, resources, stack_name):
    resource = resources.get(resource_id)
    resource_id = resource_status.get('PhysicalResourceId') or resource_id
    if not resource:
        resource = {}
    resource_type = get_resource_type(resource)
    resource_props = resource.get('Properties')
    try:
        if resource_type == 'Lambda::Function':
            resource_id = resource_props['FunctionName'] if resource else resource_id
            return aws_stack.connect_to_service('lambda').get_function(FunctionName=resource_id)
        elif resource_type == 'Lambda::Version':
            name = resource_props.get('FunctionName')
            if not name:
                return None
            func_name = aws_stack.lambda_function_name(name)
            func_version = name.split(':')[7] if len(name.split(':')) > 7 else '$LATEST'
            versions = aws_stack.connect_to_service('lambda').list_versions_by_function(FunctionName=func_name)
            return ([v for v in versions['Versions'] if v['Version'] == func_version] or [None])[0]
        elif resource_type == 'Lambda::EventSourceMapping':
            resource_id = resource_props['FunctionName'] if resource else resource_id
            source_arn = resource_props.get('EventSourceArn')
            resource_id = resolve_refs_recursively(stack_name, resource_id, resources)
            source_arn = resolve_refs_recursively(stack_name, source_arn, resources)
            if not resource_id or not source_arn:
                raise Exception('ResourceNotFound')
            mappings = aws_stack.connect_to_service('lambda').list_event_source_mappings(
                FunctionName=resource_id, EventSourceArn=source_arn)
            mapping = list(filter(lambda m:
                m['EventSourceArn'] == source_arn and m['FunctionArn'] == aws_stack.lambda_function_arn(resource_id),
                mappings['EventSourceMappings']))
            if not mapping:
                raise Exception('ResourceNotFound')
            return mapping[0]
        elif resource_type == 'DynamoDB::Table':
            resource_id = resource_props['TableName'] if resource else resource_id
            return aws_stack.connect_to_service('dynamodb').describe_table(TableName=resource_id)
        elif resource_type == 'ApiGateway::RestApi':
            apis = aws_stack.connect_to_service('apigateway').get_rest_apis()['items']
            resource_id = resource_props['Name'] if resource else resource_id
            result = list(filter(lambda api: api['name'] == resource_id, apis))
            return result[0] if result else None
        elif resource_type == 'ApiGateway::Resource':
            api_id = resource_props['RestApiId'] if resource else resource_id
            api_id = resolve_refs_recursively(stack_name, api_id, resources)
            parent_id = resolve_refs_recursively(stack_name, resource_props['ParentId'], resources)
            if not api_id or not parent_id:
                return None
            api_resources = aws_stack.connect_to_service('apigateway').get_resources(restApiId=api_id)['items']
            target_resource = list(filter(lambda res:
                res.get('parentId') == parent_id and res['pathPart'] == resource_props['PathPart'], api_resources))
            if not target_resource:
                return None
            path = aws_stack.get_apigateway_path_for_resource(api_id,
                target_resource[0]['id'], resources=api_resources)
            result = list(filter(lambda res: res['path'] == path, api_resources))
            return result[0] if result else None
        elif resource_type == 'ApiGateway::Deployment':
            api_id = resource_props['RestApiId'] if resource else resource_id
            api_id = resolve_refs_recursively(stack_name, api_id, resources)
            if not api_id:
                return None
            result = aws_stack.connect_to_service('apigateway').get_deployments(restApiId=api_id)['items']
            # TODO possibly filter results by stage name or other criteria
            return result[0] if result else None
        elif resource_type == 'ApiGateway::Method':
            api_id = resolve_refs_recursively(stack_name, resource_props['RestApiId'], resources)
            res_id = resolve_refs_recursively(stack_name, resource_props['ResourceId'], resources)
            if not api_id or not res_id:
                return None
            res_obj = aws_stack.connect_to_service('apigateway').get_resource(restApiId=api_id, resourceId=res_id)
            match = [v for (k, v) in res_obj['resourceMethods'].items()
                     if resource_props['HttpMethod'] in (v.get('httpMethod'), k)]
            int_props = resource_props.get('Integration')
            if int_props:
                match = [m for m in match if
                    m.get('methodIntegration', {}).get('type') == int_props.get('Type') and
                    m.get('methodIntegration', {}).get('httpMethod') == int_props.get('IntegrationHttpMethod')]
            return any(match) or None
        elif resource_type == 'SQS::Queue':
            sqs_client = aws_stack.connect_to_service('sqs')
            queues = sqs_client.list_queues()
            result = list(filter(lambda item:
                # TODO possibly find a better way to compare resource_id with queue URLs
                item.endswith('/%s' % resource_id), queues.get('QueueUrls', [])))
            if not result:
                return None
            result = sqs_client.get_queue_attributes(QueueUrl=result[0], AttributeNames=['All'])['Attributes']
            result['Arn'] = result['QueueArn']
            return result
        elif resource_type == 'SNS::Topic':
            topics = aws_stack.connect_to_service('sns').list_topics()
            result = list(filter(lambda item: item['TopicArn'] == resource_id, topics.get('Topics', [])))
            return result[0] if result else None
        elif resource_type == 'S3::Bucket':
            bucket_name = resource_props.get('BucketName') or resource_id
            return aws_stack.connect_to_service('s3').get_bucket_location(Bucket=bucket_name)
        elif resource_type == 'Logs::LogGroup':
            # TODO implement
            raise Exception('ResourceNotFound')
        elif resource_type == 'Kinesis::Stream':
            stream_name = resolve_refs_recursively(stack_name, resource_props['Name'], resources)
            result = aws_stack.connect_to_service('kinesis').describe_stream(StreamName=stream_name)
            return result
        elif resource_type == 'StepFunctions::StateMachine':
            sm_name = resource_props.get('StateMachineName') or resource_id
            sm_name = resolve_refs_recursively(stack_name, sm_name, resources)
            sfn_client = aws_stack.connect_to_service('stepfunctions')
            state_machines = sfn_client.list_state_machines()['stateMachines']
            sm_arn = [m['stateMachineArn'] for m in state_machines if m['name'] == sm_name]
            if not sm_arn:
                return None
            result = sfn_client.describe_state_machine(stateMachineArn=sm_arn[0])
            return result
        elif resource_type == 'StepFunctions::Activity':
            act_name = resource_props.get('Name') or resource_id
            act_name = resolve_refs_recursively(stack_name, act_name, resources)
            sfn_client = aws_stack.connect_to_service('stepfunctions')
            activities = sfn_client.list_activities()['activities']
            result = [a['activityArn'] for a in activities if a['name'] == act_name]
            if not result:
                return None
            return result[0]
        if is_deployable_resource(resource):
            LOG.warning('Unexpected resource type %s when resolving references of resource %s: %s' %
                        (resource_type, resource_id, resource))
    except Exception as e:
        check_not_found_exception(e, resource_type, resource, resource_status)
    return None


def check_not_found_exception(e, resource_type, resource, resource_status):
    # we expect this to be a "not found" exception
    markers = ['NoSuchBucket', 'ResourceNotFound', '404']
    if not list(filter(lambda marker, e=e: marker in str(e), markers)):
        LOG.warning('Unexpected error retrieving details for resource %s: %s %s - %s %s' %
            (resource_type, e, traceback.format_exc(), resource, resource_status))


def extract_resource_attribute(resource_type, resource, attribute):
    LOG.debug('Extract resource attribute: %s %s' % (resource_type, attribute))
    # extract resource specific attributes
    if resource_type == 'Lambda::Function':
        actual_attribute = 'FunctionArn' if attribute == 'Arn' else attribute
        return resource['Configuration'][actual_attribute]
    elif resource_type == 'DynamoDB::Table':
        actual_attribute = 'LatestStreamArn' if attribute == 'StreamArn' else attribute
        value = resource['Table'].get(actual_attribute)
        return value
    elif resource_type == 'ApiGateway::RestApi':
        if attribute == 'PhysicalResourceId':
            return resource['id']
        if attribute == 'RootResourceId':
            resources = aws_stack.connect_to_service('apigateway').get_resources(restApiId=resource['id'])['items']
            for res in resources:
                if res['path'] == '/' and not res.get('parentId'):
                    return res['id']
    elif resource_type == 'ApiGateway::Resource':
        if attribute == 'PhysicalResourceId':
            return resource['id']
    attribute_lower = common.first_char_to_lower(attribute)
    return resource.get(attribute) or resource.get(attribute_lower)


def resolve_ref(stack_name, ref, resources, attribute):
    if ref == 'AWS::Region':
        return aws_stack.get_region()
    resource_status = {}
    if stack_name:
        resource_status = describe_stack_resource(stack_name, ref)
        if not resource_status:
            return
        attr_value = resource_status.get(attribute)
        if attr_value not in [None, '']:
            return attr_value
    elif ref in resources:
        resource_status = resources[ref]['__details__']
    # fetch resource details
    resource = resources.get(ref)
    resource_new = retrieve_resource_details(ref, resource_status, resources, stack_name)
    if not resource_new:
        return
    resource_type = get_resource_type(resource)
    result = extract_resource_attribute(resource_type, resource_new, attribute)
    if not result:
        LOG.warning('Unable to extract reference attribute %s from resource: %s' % (attribute, resource_new))
    return result


def resolve_refs_recursively(stack_name, value, resources):
    if isinstance(value, dict):
        keys_list = list(value.keys())
        # process special operators
        if keys_list == ['Ref']:
            result = resolve_ref(stack_name, value['Ref'],
                resources, attribute='PhysicalResourceId')
            return result
        if keys_list and keys_list[0].lower() == 'fn::getatt':
            return resolve_ref(stack_name, value[keys_list[0]][0],
                resources, attribute=value[keys_list[0]][1])
        if keys_list and keys_list[0].lower() == 'fn::join':
            return value[keys_list[0]][0].join(value[keys_list[0]][1])
        if keys_list and keys_list[0].lower() == 'fn::sub':
            result = value[keys_list[0]][0]
            for key, val in value[keys_list[0]][1].items():
                val = resolve_refs_recursively(stack_name, val, resources)
                result = result.replace('${%s}' % key, val)
            return result
        else:
            for key, val in iteritems(value):
                value[key] = resolve_refs_recursively(stack_name, val, resources)
    if isinstance(value, list):
        for i in range(0, len(value)):
            value[i] = resolve_refs_recursively(stack_name, value[i], resources)
    return value


def update_resource(resource_id, resources, stack_name):
    resource = resources[resource_id]
    resource_type = get_resource_type(resource)
    if resource_type not in UPDATEABLE_RESOURCES:
        LOG.warning('Unable to update resource type "%s", id "%s"' % (resource_type, resource_id))
        return
    LOG.info('Updating resource %s of type %s' % (resource_id, resource_type))
    props = resource['Properties']
    if resource_type == 'Lambda::Function':
        client = aws_stack.connect_to_service('lambda')
        keys = ('FunctionName', 'Role', 'Handler', 'Description', 'Timeout', 'MemorySize', 'Environment', 'Runtime')
        update_props = dict([(k, props[k]) for k in keys if k in props])
        update_props = resolve_refs_recursively(stack_name, update_props, resources)
        if 'Code' in props:
            client.update_function_code(FunctionName=props['FunctionName'], **props['Code'])
        return client.update_function_configuration(**update_props)
    if resource_type == 'ApiGateway::Method':
        client = aws_stack.connect_to_service('apigateway')
        integration = props.get('Integration')
        # TODO use RESOURCE_TO_FUNCTION mechanism for updates, instead of hardcoding here
        kwargs = {
            'restApiId': props['RestApiId'],
            'resourceId': props['ResourceId'],
            'httpMethod': props['HttpMethod'],
            'requestParameters': props.get('RequestParameters')
        }
        if integration:
            kwargs['type'] = integration['Type']
            kwargs['integrationHttpMethod'] = integration.get('IntegrationHttpMethod')
            kwargs['uri'] = integration.get('Uri')
            return client.put_integration(**kwargs)
        kwargs['authorizationType'] = props.get('AuthorizationType')
        return client.put_method(**kwargs)


def fix_account_id_in_arns(params):
    def fix_ids(o, **kwargs):
        if isinstance(o, dict):
            for k, v in o.items():
                if common.is_string(v, exclude_binary=True):
                    o[k] = aws_stack.fix_account_id_in_arns(v)
        return o
    result = common.recurse_object(params, fix_ids)
    return result


def convert_data_types(func_details, params):
    """ Convert data types in the "params" object, with the type defs
        specified in the 'types' attribute of "func_details". """
    types = func_details.get('types') or {}
    attr_names = types.keys() or []

    def cast(_obj, _type):
        if _type == bool:
            return _obj in ['True', 'true', True]
        if _type == str:
            return str(_obj)
        if _type == int:
            return int(_obj)
        return _obj

    def fix_types(o, **kwargs):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in attr_names:
                    o[k] = cast(v, types[k])
        return o
    result = common.recurse_object(params, fix_types)
    return result


def remove_none_values(params):
    """ Remove None values recursively in the given object. """
    def remove_nones(o, **kwargs):
        if isinstance(o, dict):
            for k, v in dict(o).items():
                if v is None:
                    o.pop(k)
        return o
    result = common.recurse_object(params, remove_nones)
    return result


def deploy_resource(resource_id, resources, stack_name):
    return execute_resource_action(resource_id, resources, stack_name, ACTION_CREATE)


def delete_resource(resource_id, resources, stack_name):
    return execute_resource_action(resource_id, resources, stack_name, ACTION_DELETE)


def execute_resource_action(resource_id, resources, stack_name, action_name):
    resource = resources[resource_id]
    resource_type = get_resource_type(resource)
    func_details = RESOURCE_TO_FUNCTION.get(resource_type)
    if not func_details or action_name not in func_details:
        LOG.warning('Action "%s" for resource type %s not yet implemented' % (action_name, resource_type))
        return

    LOG.debug('Running action "%s" for resource type "%s" id "%s"' % (action_name, resource_type, resource_id))
    func_details = func_details[action_name]
    func_details = func_details if isinstance(func_details, list) else [func_details]
    results = []
    for func in func_details:
        if callable(func['function']):
            result = func['function'](resource_id, resources, resource_type, func, stack_name)
            results.append(result)
            continue
        client = get_client(resource, func)
        if client:
            result = configure_resource_via_sdk(resource_id, resources, resource_type, func, stack_name)
            results.append(result)
    return (results or [None])[0]


def configure_resource_via_sdk(resource_id, resources, resource_type, func_details, stack_name):
    resource = resources[resource_id]
    client = get_client(resource, func_details)
    function = getattr(client, func_details['function'])
    params = func_details.get('parameters') or lambda_get_params()
    defaults = func_details.get('defaults', {})
    if 'Properties' not in resource:
        resource['Properties'] = {}
    resource_props = resource['Properties']

    if callable(params):
        params = params(resource_props, stack_name=stack_name, resources=resources)
    else:
        params = dict(params)
        for param_key, prop_keys in dict(params).items():
            params.pop(param_key, None)
            if not isinstance(prop_keys, list):
                prop_keys = [prop_keys]
            for prop_key in prop_keys:
                if prop_key == PLACEHOLDER_RESOURCE_NAME:
                    params[param_key] = resource_id
                    resource_name = get_resource_name(resource)
                    if resource_name:
                        params[param_key] = resource_name
                    else:
                        # try to obtain physical resource name from stack resources
                        try:
                            return resolve_ref(stack_name, resource_id, resources,
                                attribute='PhysicalResourceId')
                        except Exception as e:
                            LOG.debug('Unable to extract physical id for resource %s: %s' % (resource_id, e))

                else:
                    if callable(prop_key):
                        prop_value = prop_key(resource_props, stack_name=stack_name, resources=resources)
                    else:
                        prop_value = resource_props.get(prop_key)
                    if prop_value is not None:
                        params[param_key] = prop_value

    # assign default value if empty
    params = common.merge_recursive(defaults, params)

    # convert refs and boolean strings
    for param_key, param_value in dict(params).items():
        if param_value is not None:
            param_value = params[param_key] = resolve_refs_recursively(stack_name, param_value, resources)
        # Convert to boolean (TODO: do this recursively?)
        if str(param_value).lower() in ['true', 'false']:
            params[param_key] = str(param_value).lower() == 'true'

    # convert any moto account IDs (123456789012) in ARNs to our format (000000000000)
    params = fix_account_id_in_arns(params)
    # convert data types (e.g., boolean strings to bool)
    params = convert_data_types(func_details, params)
    # remove None values, as they usually raise boto3 errors
    params = remove_none_values(params)

    # invoke function
    try:
        LOG.debug('Request for resource type "%s" in region %s: %s %s' % (
            resource_type, aws_stack.get_region(), func_details['function'], params))
        result = function(**params)
    except Exception as e:
        LOG.warning('Error calling %s with params: %s for resource: %s' % (function, params, resource))
        raise e

    # some resources have attached/nested resources which we need to create recursively now
    if resource_type == 'ApiGateway::Method':
        integration = resource_props.get('Integration')
        if integration:
            api_id = resolve_refs_recursively(stack_name, resource_props['RestApiId'], resources)
            res_id = resolve_refs_recursively(stack_name, resource_props['ResourceId'], resources)
            uri = integration.get('Uri')
            if uri:
                uri = resolve_refs_recursively(stack_name, uri, resources)
                aws_stack.connect_to_service('apigateway').put_integration(restApiId=api_id, resourceId=res_id,
                    httpMethod=resource_props['HttpMethod'], type=integration['Type'],
                    integrationHttpMethod=integration['IntegrationHttpMethod'], uri=uri
                )
    elif resource_type == 'SNS::Topic':
        subscriptions = resource_props.get('Subscription', [])
        for subscription in subscriptions:
            endpoint = resolve_refs_recursively(stack_name, subscription['Endpoint'], resources)
            topic_arn = retrieve_topic_arn(params['Name'])
            aws_stack.connect_to_service('sns').subscribe(
                TopicArn=topic_arn, Protocol=subscription['Protocol'], Endpoint=endpoint)
    elif resource_type == 'S3::Bucket':
        tags = resource_props.get('Tags')
        if tags:
            aws_stack.connect_to_service('s3').put_bucket_tagging(
                Bucket=params['Bucket'], Tagging={'TagSet': tags})

    return result


# TODO remove?
def deploy_template(template, stack_name):
    if isinstance(template, string_types):
        template = parse_template(template)

    resource_map = template.get('Resources')
    if not resource_map:
        LOG.warning('CloudFormation template contains no Resources section')
        return

    next = resource_map

    iters = 10
    for i in range(0, iters):

        # get resource details
        for resource_id, resource in next.items():
            stack_resource = describe_stack_resource(stack_name, resource_id)
            resource['__details__'] = stack_resource

        next = resources_to_deploy_next(resource_map, stack_name)
        if not next:
            return

        for resource_id, resource in next.items():
            deploy_resource(resource_id, resource_map, stack_name=stack_name)

    LOG.warning('Unable to resolve all dependencies and deploy all resources ' +
        'after %s iterations. Remaining (%s): %s' % (iters, len(next), next))


def delete_stack(stack_name, stack_resources):
    resources = dict([(r['LogicalResourceId'], common.clone_safe(r)) for r in stack_resources])
    for key, resource in resources.items():
        resources[key]['Properties'] = common.clone_safe(resource)
    for resource_id, resource in resources.items():
        delete_resource(resource_id, resources, stack_name)


# --------
# Util methods for analyzing resource dependencies
# --------

def is_deployable_resource(resource):
    resource_type = get_resource_type(resource)
    entry = RESOURCE_TO_FUNCTION.get(resource_type)
    if entry is None:
        LOG.warning('Unknown resource type "%s": %s' % (resource_type, resource))
    return bool(entry and entry.get(ACTION_CREATE))


def is_deployed(resource_id, resources, stack_name):
    resource = resources[resource_id]
    resource_status = resource.get('__details__') or {}
    details = retrieve_resource_details(resource_id, resource_status, resources, stack_name)
    return bool(details)


def should_be_deployed(resource_id, resources, stack_name):
    """ Return whether the given resource is all of: (1) deployable, (2) not yet deployed,
        and (3) has no unresolved dependencies. """
    resource = resources[resource_id]
    if not is_deployable_resource(resource) or is_deployed(resource_id, resources, stack_name):
        return False
    res_deps = get_resource_dependencies(resource_id, resource, resources)
    return all_dependencies_satisfied(res_deps, stack_name, resources, resource_id)


def is_updateable(resource_id, resources, stack_name):
    """ Return whether the given resource can be updated or not """
    resource = resources[resource_id]
    if not is_deployable_resource(resource) or not is_deployed(resource_id, resources, stack_name):
        return False
    resource_type = get_resource_type(resource)
    return resource_type in UPDATEABLE_RESOURCES


def all_dependencies_satisfied(resources, stack_name, all_resources, depending_resource=None):
    for resource_id, resource in iteritems(resources):
        if is_deployable_resource(resource):
            if not is_deployed(resource_id, all_resources, stack_name):
                return False
    return True


def resources_to_deploy_next(resources, stack_name):
    result = {}
    for resource_id, resource in resources.items():
        if should_be_deployed(resource_id, resources, stack_name):
            result[resource_id] = resource
    return result


def get_resource_dependencies(resource_id, resource, resources):
    result = {}
    dumped = json.dumps(common.json_safe(resource))
    dependencies = resource.get('DependsOn', [])
    dependencies = dependencies if isinstance(dependencies, list) else [dependencies]
    for other_id, other in resources.items():
        if resource != other:
            # TODO: traverse dict instead of doing string search
            search1 = '{"Ref": "%s"}' % other_id
            search2 = '{"Fn::GetAtt": ["%s", ' % other_id
            if search1 in dumped or search2 in dumped:
                result[other_id] = other
            if other_id in dependencies:
                result[other_id] = other
    return result
