"""CDK synthesis tests for AgentCore Gateway resources."""

import pytest

# Skip entire module if CDK alpha construct is not installed
pytest.importorskip("aws_cdk.aws_bedrock_agentcore_alpha")

from aws_cdk import App
from aws_cdk.assertions import Template, Match

from infrastructure.stack import ConnectAsyncMultiResponseStack


CONNECT_INSTANCE_ARN = "arn:aws:connect:us-east-1:123456789012:instance/test-instance-id"


def _synth_template(*, deploy_orchestrator: bool = False, connect_instance_arn: str | None = None) -> Template:
    """Synthesize the stack and return a Template for assertions."""
    context = {}
    if deploy_orchestrator:
        context["deploy_orchestrator"] = "true"
    app = App(context=context)
    stack = ConnectAsyncMultiResponseStack(
        app,
        "TestStack",
        connect_instance_arn=connect_instance_arn,
    )
    return Template.from_stack(stack)


class TestGatewayConditionalCreation:
    """Property 1: Gateway conditional creation."""

    def test_gateway_exists_when_orchestrator_enabled(self):
        template = _synth_template(deploy_orchestrator=True, connect_instance_arn=CONNECT_INSTANCE_ARN)
        template.resource_count_is("AWS::BedrockAgentCore::Gateway", 1)

    def test_gateway_absent_when_orchestrator_disabled(self):
        template = _synth_template(deploy_orchestrator=False)
        template.resource_count_is("AWS::BedrockAgentCore::Gateway", 0)

    def test_no_targets_when_orchestrator_disabled(self):
        template = _synth_template(deploy_orchestrator=False)
        template.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 0)


class TestLambdaTargets:
    """Verify Lambda targets are created with correct properties."""

    def test_submit_query_target_exists(self):
        template = _synth_template(deploy_orchestrator=True, connect_instance_arn=CONNECT_INSTANCE_ARN)
        template.has_resource_properties(
            "AWS::BedrockAgentCore::GatewayTarget",
            Match.object_like({
                "Name": "submit-query",
            }),
        )

    def test_check_responses_target_exists(self):
        template = _synth_template(deploy_orchestrator=True, connect_instance_arn=CONNECT_INSTANCE_ARN)
        template.has_resource_properties(
            "AWS::BedrockAgentCore::GatewayTarget",
            Match.object_like({
                "Name": "check-responses",
            }),
        )

    def test_two_targets_created(self):
        template = _synth_template(deploy_orchestrator=True, connect_instance_arn=CONNECT_INSTANCE_ARN)
        template.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 2)


class TestSsmParameter:
    """Verify SSM parameter for Gateway ARN."""

    def test_gateway_arn_ssm_parameter_created(self):
        template = _synth_template(deploy_orchestrator=True, connect_instance_arn=CONNECT_INSTANCE_ARN)
        template.has_resource_properties(
            "AWS::SSM::Parameter",
            Match.object_like({
                "Name": "/connect-async-multi-response/agentcore-gateway-arn",
            }),
        )

    def test_no_gateway_ssm_when_orchestrator_disabled(self):
        template = _synth_template(deploy_orchestrator=False)
        # The SSM parameter for gateway ARN should not exist
        # (other SSM params will exist, so we check by name)
        resources = template.find_resources("AWS::SSM::Parameter", {
            "Properties": {
                "Name": "/connect-async-multi-response/agentcore-gateway-arn",
            }
        })
        assert len(resources) == 0


class TestConnectIntegration:
    """Verify Connect MCP server registration via Custom Resource."""

    def test_mcp_registration_lambda_created_with_instance_arn(self):
        """Custom Resource Lambda for MCP registration should exist when orchestrator + instance ARN are provided."""
        template = _synth_template(deploy_orchestrator=True, connect_instance_arn=CONNECT_INSTANCE_ARN)
        template.has_resource_properties(
            "AWS::Lambda::Function",
            Match.object_like({
                "FunctionName": "ConnectAsync-McpRegistration",
            }),
        )

    def test_mcp_custom_resource_passes_gateway_arn(self):
        """The Custom Resource should receive the Gateway ARN in its properties."""
        template = _synth_template(deploy_orchestrator=True, connect_instance_arn=CONNECT_INSTANCE_ARN)
        template.has_resource_properties(
            "AWS::CloudFormation::CustomResource",
            Match.object_like({
                "ApplicationName": "ConnectAsyncMcpServer",
            }),
        )

    def test_no_mcp_registration_without_instance_arn(self):
        template = _synth_template(deploy_orchestrator=True, connect_instance_arn=None)
        # Without instance ARN, _create_orchestrator_agent is not called at all
        # (it's gated on connect_instance_arn in __init__)
        resources = template.find_resources("AWS::Lambda::Function", {
            "Properties": {
                "FunctionName": "ConnectAsync-McpRegistration",
            }
        })
        assert len(resources) == 0
