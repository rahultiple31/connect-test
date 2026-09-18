"""CDK stack for the Connect async multi-response pattern.

Defines all AWS resources: DynamoDB table, Lambda functions,
API Gateway, IAM roles, SSM parameters, and Secrets Manager secrets.
"""

from __future__ import annotations

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_connect as connect,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_lex as lex,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
    aws_wisdom as wisdom,
)
from constructs import Construct
import jsii
from aws_cdk import ILocalBundling


@jsii.implements(ILocalBundling)
class _LocalBundler:
    """Local bundler for Lambda code — installs pip deps without Docker."""

    def try_bundle(self, output_dir: str, *, image=None, **kwargs) -> bool:
        import os
        import shutil
        import subprocess

        source = "lambdas/custom_resources"
        req = f"{source}/requirements.txt"
        subprocess.check_call(
            ["pip", "install", "-r", req, "-t", output_dir, "--quiet",
             "--platform", "manylinux2014_x86_64", "--only-binary=:all:"],
        )
        for item in ("mcp_server_registration.py", "__init__.py"):
            src = f"{source}/{item}"
            if os.path.exists(src):
                shutil.copy2(src, output_dir)
        return True


@jsii.implements(ILocalBundling)
class _SharedLayerBundler:
    """Local bundler for the shared Lambda layer.

    Creates the correct directory structure so ``from lambdas.shared.X``
    imports work at runtime:
        python/lambdas/__init__.py
        python/lambdas/shared/__init__.py
        python/lambdas/shared/config.py
        python/lambdas/shared/models.py
        ...
    Also installs pydantic (required by models.py).
    """

    def try_bundle(self, output_dir: str, *, image=None, **kwargs) -> bool:
        import os
        import shutil
        import subprocess

        target = os.path.join(output_dir, "python")
        shared_target = os.path.join(target, "lambdas", "shared")
        os.makedirs(shared_target, exist_ok=True)

        # Install pydantic
        subprocess.check_call(
            ["pip", "install", "pydantic>=2.0", "-t", target, "--quiet",
             "--platform", "manylinux2014_x86_64",
             "--implementation", "cp",
             "--python-version", "3.12",
             "--only-binary=:all:"],
        )

        # Copy lambdas/__init__.py
        shutil.copy2("lambdas/__init__.py", os.path.join(target, "lambdas", "__init__.py"))

        # Copy lambdas/shared/*.py
        for f in os.listdir("lambdas/shared"):
            if f.endswith(".py"):
                shutil.copy2(os.path.join("lambdas", "shared", f), shared_target)

        return True


class ConnectAsyncMultiResponseStack(Stack):
    """Full infrastructure stack for the async multi-response pattern."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        connect_instance_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Resolve Connect instance ARN from constructor arg or CDK context
        self._connect_instance_arn = (
            connect_instance_arn
            or self.node.try_get_context("connect_instance_arn")
        )

        # ---------------------------------------------------------------
        # Task 9.1 — KMS key + DynamoDB table
        # ---------------------------------------------------------------
        self.kms_key = kms.Key(
            self,
            "ResponseTableKey",
            description="KMS key for AsyncResponseQueue DynamoDB encryption",
            enable_key_rotation=True,
            removal_policy=removal_policy,
        )

        self.response_table = dynamodb.Table(
            self,
            "AsyncResponseQueue",
            partition_key=dynamodb.Attribute(
                name="SessionID", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SequenceNumber", type=dynamodb.AttributeType.NUMBER
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            time_to_live_attribute="TTL",
            removal_policy=removal_policy,
        )

        self.response_table.add_global_secondary_index(
            index_name="SessionStatusIndex",
            partition_key=dynamodb.Attribute(
                name="SessionStatus", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="CreatedAt", type=dynamodb.AttributeType.NUMBER
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ---------------------------------------------------------------
        # VPC for Submit Lambda (NAT Gateway for static outbound IP)
        # ---------------------------------------------------------------
        self.vpc = ec2.Vpc(
            self,
            "SubmitLambdaVpc",
            vpc_name="ConnectAsync-SubmitLambdaVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # DynamoDB gateway endpoint (free — avoids NAT for DynamoDB traffic)
        self.vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
        )

        # Security group for Submit Lambda — egress HTTPS only
        self.submit_sg = ec2.SecurityGroup(
            self,
            "SubmitLambdaSg",
            vpc=self.vpc,
            description="Submit Lambda - egress HTTPS to Nexthink and AWS APIs via NAT",
            allow_all_outbound=False,
        )
        self.submit_sg.add_egress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS outbound to Nexthink and AWS APIs",
        )

        # Output the NAT Gateway Elastic IP (whitelist this on Nexthink API GW)
        nat_gw_eips = [
            gw.node.default_child
            for gw in self.vpc.node.find_all()
            if isinstance(gw, ec2.CfnNatGateway)
        ]
        # The VPC construct creates a CfnEIP associated with each NAT GW.
        # We can get the allocation from the NAT gateway's public subnets.
        for i, public_subnet in enumerate(self.vpc.public_subnets):
            for child in public_subnet.node.children:
                if isinstance(child, ec2.CfnEIP):
                    CfnOutput(
                        self,
                        f"NatGatewayEip{i}",
                        value=child.ref,
                        description=f"NAT Gateway Elastic IP (AZ {i}) — whitelist on Nexthink API GW",
                    )

        # ---------------------------------------------------------------
        # Task 9.4 — SSM parameters (configurable defaults)
        # ---------------------------------------------------------------
        ssm_params: dict[str, str] = {
            "polling-interval-ms": "3000",
            "max-session-duration-s": "300",
            "initial-response-timeout-s": "30",
            "hold-message-pool": '["I\'m still working on that for you.", "Just a moment while I gather more information.", "Thank you for your patience, still processing your request."]',
            "polly-voice-id": "Matthew",
            "nova-sonic-voice": "Matthew",
            "max-response-length": "3000",
            "silence-timeout-s": "4",
            "max-poll-iterations": "10",
        }

        # Only create the placeholder NEXTHINK_AGENT_URL if the mock is NOT deployed
        # (the mock creates its own SSM parameter pointing to the mock API)
        if self.node.try_get_context("deploy_mock_nexthink") != "true":
            ssm_params["nexthink-agent-url"] = "https://placeholder.nexthink.example.com/api/agent"

        # ---------------------------------------------------------------
        # Nexthink backend selection: "mock" (default) or "spark" (real A2A API)
        # Pass via: -c nexthink_backend=spark -c nexthink_tenant_id=... etc.
        # (scripts/deploy.sh reads these from .env)
        # Values land in SSM; the Submit Lambda reads them via Config.
        # ---------------------------------------------------------------
        self.nexthink_backend: str = (
            self.node.try_get_context("nexthink_backend") or "mock"
        ).lower()
        if self.nexthink_backend not in ("mock", "spark"):
            raise ValueError(
                f"nexthink_backend must be 'mock' or 'spark', got {self.nexthink_backend!r}"
            )
        ssm_params["nexthink-backend"] = self.nexthink_backend

        _spark_ctx = {
            "nexthink-tenant-id": "nexthink_tenant_id",
            "nexthink-token-url": "nexthink_token_url",
            "nexthink-spark-url": "nexthink_spark_url",
            "nexthink-user-principal": "nexthink_user_principal",
        }
        for ssm_name, ctx_key in _spark_ctx.items():
            value = self.node.try_get_context(ctx_key)
            if self.nexthink_backend == "spark" and not value:
                raise ValueError(
                    f"nexthink_backend=spark requires -c {ctx_key}=... "
                    f"(set {ctx_key.upper()} in .env)"
                )
            # Only create the param when provided; Config falls back to "" otherwise.
            if value:
                ssm_params[ssm_name] = value

        self.ssm_parameters: dict[str, ssm.StringParameter] = {}
        ssm_prefix = "/connect-async-multi-response"
        for name, default in ssm_params.items():
            param = ssm.StringParameter(
                self,
                f"Param{name}",
                parameter_name=f"{ssm_prefix}/{name}",
                string_value=default,
                description=f"Config: {name}",
            )
            self.ssm_parameters[name] = param

        # Secrets Manager secrets (placeholder values)
        self.nexthink_api_key_secret = secretsmanager.Secret(
            self,
            "NexThinkApiKey",
            secret_name="connect-async/NEXTHINK_API_KEY",
            description="API key for the Nexthink AI agent",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"api_key": "PLACEHOLDER"}',
                generate_string_key="generated",
            ),
        )

        self.callback_api_key_secret = secretsmanager.Secret(
            self,
            "CallbackApiKey",
            secret_name="connect-async/CALLBACK_API_KEY",
            description="API key for the callback endpoint",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"api_key": "PLACEHOLDER"}',
                generate_string_key="generated",
            ),
        )

        # ---------------------------------------------------------------
        # Task 9.3 — Shared Lambda layer + Lambda functions
        # ---------------------------------------------------------------
        shared_layer = lambda_.LayerVersion(
            self,
            "SharedLayer",
            code=lambda_.Code.from_asset(
                "lambdas",
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=["echo", "Docker bundling not used"],
                    local=_SharedLayerBundler(),
                ),
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Shared module: session_manager, config, models, tts + pydantic",
        )

        common_env = {
            "RESPONSE_TABLE_NAME": self.response_table.table_name,
            "KMS_KEY_ID": self.kms_key.key_id,
            "SSM_PREFIX": ssm_prefix,
            "NEXTHINK_API_KEY_SECRET_NAME": self.nexthink_api_key_secret.secret_name,
            "CALLBACK_API_KEY_SECRET_NAME": self.callback_api_key_secret.secret_name,
        }

        # --- Callback Lambda ---
        callback_log_group = logs.LogGroup(
            self,
            "CallbackLogGroup",
            log_group_name="/aws/lambda/ConnectAsync-Callback",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=removal_policy,
        )
        self.callback_lambda = lambda_.Function(
            self,
            "CallbackLambda",
            function_name="ConnectAsync-Callback",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/callback"),
            layers=[shared_layer],
            memory_size=256,
            timeout=Duration.seconds(10),
            environment=common_env,
            log_group=callback_log_group,
        )

        # --- Polling Lambda ---
        polling_log_group = logs.LogGroup(
            self,
            "PollingLogGroup",
            log_group_name="/aws/lambda/ConnectAsync-Polling",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=removal_policy,
        )
        self.polling_lambda = lambda_.Function(
            self,
            "PollingLambda",
            function_name="ConnectAsync-Polling",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/polling"),
            layers=[shared_layer],
            memory_size=256,
            timeout=Duration.seconds(8),
            environment=common_env,
            log_group=polling_log_group,
        )

        # --- Submit Lambda ---
        submit_log_group = logs.LogGroup(
            self,
            "SubmitLogGroup",
            log_group_name="/aws/lambda/ConnectAsync-Submit",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=removal_policy,
        )
        self.submit_lambda = lambda_.Function(
            self,
            "SubmitLambda",
            function_name="ConnectAsync-Submit",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/submit"),
            layers=[shared_layer],
            memory_size=256,
            timeout=Duration.seconds(8),
            environment=common_env,
            log_group=submit_log_group,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
            security_groups=[self.submit_sg],
        )

        # --- Disconnect Lambda ---
        disconnect_log_group = logs.LogGroup(
            self,
            "DisconnectLogGroup",
            log_group_name="/aws/lambda/ConnectAsync-Disconnect",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=removal_policy,
        )
        self.disconnect_lambda = lambda_.Function(
            self,
            "DisconnectLambda",
            function_name="ConnectAsync-Disconnect",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/disconnect"),
            layers=[shared_layer],
            memory_size=256,
            timeout=Duration.seconds(10),
            environment=common_env,
            log_group=disconnect_log_group,
        )

        # ---------------------------------------------------------------
        # IAM — least-privilege per Lambda
        # ---------------------------------------------------------------

        # Callback Lambda: PutItem, UpdateItem, GetItem on table + PutMetricData
        self.callback_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:GetItem",
                ],
                resources=[
                    self.response_table.table_arn,
                ],
            )
        )
        self.callback_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": "ConnectAsyncMultiResponse"
                    }
                },
            )
        )

        # Polling Lambda: Query, UpdateItem, GetItem on table (+ GSI) + PutMetricData
        self.polling_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:Query",
                    "dynamodb:UpdateItem",
                    "dynamodb:GetItem",
                ],
                resources=[
                    self.response_table.table_arn,
                    f"{self.response_table.table_arn}/index/SessionStatusIndex",
                ],
            )
        )
        self.polling_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": "ConnectAsyncMultiResponse"
                    }
                },
            )
        )

        # Submit Lambda: PutItem, DeleteItem on table + Secrets Manager + PutMetricData
        self.submit_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[
                    self.response_table.table_arn,
                ],
            )
        )
        self.submit_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": "ConnectAsyncMultiResponse"
                    }
                },
            )
        )
        self.nexthink_api_key_secret.grant_read(self.submit_lambda)

        # All Lambdas load config eagerly (including secrets), so grant read
        # on both secrets to all Lambdas
        for fn in [self.callback_lambda, self.polling_lambda,
                   self.submit_lambda, self.disconnect_lambda]:
            self.nexthink_api_key_secret.grant_read(fn)
            self.callback_api_key_secret.grant_read(fn)

        # Disconnect Lambda: GetItem, UpdateItem on table + PutMetricData
        self.disconnect_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:UpdateItem",
                ],
                resources=[
                    self.response_table.table_arn,
                ],
            )
        )
        self.disconnect_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": "ConnectAsyncMultiResponse"
                    }
                },
            )
        )

        # KMS decrypt for all Lambdas (needed to read/write encrypted table)
        self.kms_key.grant_encrypt_decrypt(self.callback_lambda)
        self.kms_key.grant_encrypt_decrypt(self.polling_lambda)
        self.kms_key.grant_encrypt_decrypt(self.submit_lambda)
        self.kms_key.grant_encrypt_decrypt(self.disconnect_lambda)

        # SSM parameter read for all Lambdas
        for param in self.ssm_parameters.values():
            param.grant_read(self.callback_lambda)
            param.grant_read(self.polling_lambda)
            param.grant_read(self.submit_lambda)
            param.grant_read(self.disconnect_lambda)

        # SSM GetParametersByPath — the config loader uses this to load all
        # params under the prefix in one call
        ssm_path_policy = iam.PolicyStatement(
            actions=["ssm:GetParametersByPath"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{ssm_prefix}",
                f"arn:aws:ssm:{self.region}:{self.account}:parameter{ssm_prefix}/*",
            ],
        )
        self.callback_lambda.add_to_role_policy(ssm_path_policy)
        self.polling_lambda.add_to_role_policy(ssm_path_policy)
        self.submit_lambda.add_to_role_policy(ssm_path_policy)
        self.disconnect_lambda.add_to_role_policy(ssm_path_policy)

        # ---------------------------------------------------------------
        # Task 9.2 — API Gateway REST API
        # ---------------------------------------------------------------
        # The callback endpoint accepts TWO payload shapes (the Lambda sniffs
        # which one arrived — see lambdas/callback/handler.py):
        #   1. Native: {sessionId, sequenceNumber, responseText, isComplete}
        #   2. Nexthink Spark A2A: {statusUpdate: {contextId, status: {...}}}
        # Draft-4 `oneOf` keeps API Gateway validation strict for both.
        native_callback_schema = apigw.JsonSchema(
            type=apigw.JsonSchemaType.OBJECT,
            required=["sessionId", "sequenceNumber", "responseText", "isComplete"],
            properties={
                "sessionId": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, min_length=1),
                "sequenceNumber": apigw.JsonSchema(type=apigw.JsonSchemaType.INTEGER, minimum=1),
                "responseText": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, min_length=1),
                "isComplete": apigw.JsonSchema(type=apigw.JsonSchemaType.BOOLEAN),
                "metadata": apigw.JsonSchema(
                    type=apigw.JsonSchemaType.OBJECT,
                    properties={
                        "sourceAgent": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING),
                        "confidence": apigw.JsonSchema(type=apigw.JsonSchemaType.NUMBER),
                    },
                ),
            },
        )
        spark_callback_schema = apigw.JsonSchema(
            type=apigw.JsonSchemaType.OBJECT,
            required=["statusUpdate"],
            properties={
                "statusUpdate": apigw.JsonSchema(
                    type=apigw.JsonSchemaType.OBJECT,
                    required=["contextId", "status"],
                    properties={
                        "contextId": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, min_length=1),
                        "taskId": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING),
                        "status": apigw.JsonSchema(type=apigw.JsonSchemaType.OBJECT),
                    },
                ),
            },
        )
        callback_request_model = {
            "schema": apigw.JsonSchemaVersion.DRAFT4,
            "title": "CallbackPayload",
            "one_of": [native_callback_schema, spark_callback_schema],
        }

        self.api = apigw.RestApi(
            self,
            "CallbackApi",
            rest_api_name="ConnectAsyncCallbackApi",
            description="Callback endpoint for Nexthink AI agent responses",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                throttling_rate_limit=100,
                throttling_burst_limit=200,
                logging_level=apigw.MethodLoggingLevel.INFO,
            ),
        )

        request_model = self.api.add_model(
            "CallbackRequestModel",
            content_type="application/json",
            model_name="CallbackPayload",
            schema=apigw.JsonSchema(**callback_request_model),
        )

        request_validator = self.api.add_request_validator(
            "BodyValidator",
            validate_request_body=True,
            validate_request_parameters=False,
        )

        callback_resource = self.api.root.add_resource("callback")
        callback_integration = apigw.LambdaIntegration(self.callback_lambda)

        # ---------------------------------------------------------------
        # !! SECURITY NOTE — callback authentication vs. backend !!
        #
        # mock backend : API key REQUIRED. The mock agent sends the key as
        #                `x-api-key` on every callback.
        # spark backend: API key DISABLED. Nexthink Spark receives our key in
        #                `configuration.pushNotification.token` but does NOT
        #                replay it as an `x-api-key` header — every callback
        #                would be rejected with 403. This mirrors what was
        #                deployed in the Nexthink prototype environment.
        #
        # In spark mode the /callback endpoint is therefore reachable by anyone
        # who knows the URL and a live sessionId. Before production, replace
        # with one of:
        #   (a) a Lambda/custom authorizer that validates however Spark does
        #       transmit the pushNotification token (header/body — confirm
        #       with Nexthink), or
        #   (b) resource policy / IP allow-list restricted to Nexthink's
        #       egress ranges.
        # ---------------------------------------------------------------
        callback_api_key_required = self.nexthink_backend != "spark"

        callback_resource.add_method(
            "POST",
            callback_integration,
            api_key_required=callback_api_key_required,
            request_models={"application/json": request_model},
            request_validator=request_validator,
        )

        # API key + usage plan
        api_key = self.api.add_api_key(
            "CallbackApiKey",
            api_key_name="ConnectAsyncCallbackApiKey",
        )

        usage_plan = self.api.add_usage_plan(
            "CallbackUsagePlan",
            name="ConnectAsyncCallbackUsagePlan",
            throttle=apigw.ThrottleSettings(rate_limit=100, burst_limit=200),
        )
        usage_plan.add_api_stage(stage=self.api.deployment_stage)
        usage_plan.add_api_key(api_key)

        # Add the callback API URL to the Submit Lambda so it can pass it
        # to Nexthink (or mock) when the Orchestrator doesn't provide one
        self.submit_lambda.add_environment(
            "CALLBACK_API_URL",
            Fn.join("", [self.api.url, "callback"]),
        )

        # ---------------------------------------------------------------
        # Lex V2 Bots (Option A)
        # ---------------------------------------------------------------

        # IAM role for Lex bots
        self.lex_bot_role = iam.Role(
            self,
            "LexBotRole",
            assumed_by=iam.ServicePrincipal("lexv2.amazonaws.com"),
            inline_policies={
                "LexRuntimePolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "polly:SynthesizeSpeech",
                                "comprehend:DetectSentiment",
                            ],
                            resources=["*"],
                        ),
                    ],
                ),
            },
        )

        # --- Interrupt Bot (AsyncResponseInterruptBot) ---
        self.interrupt_bot = lex.CfnBot(
            self,
            "InterruptBot",
            name="AsyncResponseInterruptBot",
            role_arn=self.lex_bot_role.role_arn,
            data_privacy={"ChildDirected": False},
            idle_session_ttl_in_seconds=300,
            description=(
                "Lex bot for Option A polling loop — detects caller "
                "interruptions (cancel, new question, repeat) during hold."
            ),
            auto_build_bot_locales=True,
            bot_locales=[
                lex.CfnBot.BotLocaleProperty(
                    locale_id="en_US",
                    nlu_confidence_threshold=0.40,
                    voice_settings=lex.CfnBot.VoiceSettingsProperty(
                        voice_id="Matthew",
                    ),
                    intents=[
                        # CancelQuery
                        lex.CfnBot.IntentProperty(
                            name="CancelQuery",
                            description="Caller wants to cancel the current query.",
                            sample_utterances=[
                                lex.CfnBot.SampleUtteranceProperty(utterance="cancel"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="stop"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="never mind"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="forget it"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="cancel my request"),
                            ],
                        ),
                        # AskNewQuestion
                        lex.CfnBot.IntentProperty(
                            name="AskNewQuestion",
                            description="Caller wants to ask a different question.",
                            sample_utterances=[
                                lex.CfnBot.SampleUtteranceProperty(utterance="new question"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="ask something else"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="different question"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="I have another question"),
                            ],
                        ),
                        # RepeatLastResponse
                        lex.CfnBot.IntentProperty(
                            name="RepeatLastResponse",
                            description="Caller wants to hear the last response again.",
                            sample_utterances=[
                                lex.CfnBot.SampleUtteranceProperty(utterance="repeat"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="say that again"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="what did you say"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="repeat that"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="can you repeat"),
                            ],
                        ),
                        # FallbackIntent (required)
                        lex.CfnBot.IntentProperty(
                            name="FallbackIntent",
                            description="Catches unrecognized speech and silence timeout.",
                            parent_intent_signature="AMAZON.FallbackIntent",
                        ),
                    ],
                ),
            ],
        )

        self.interrupt_bot_version = lex.CfnBotVersion(
            self,
            "InterruptBotVersion",
            bot_id=self.interrupt_bot.attr_id,
            bot_version_locale_specification=[
                lex.CfnBotVersion.BotVersionLocaleSpecificationProperty(
                    locale_id="en_US",
                    bot_version_locale_details=lex.CfnBotVersion.BotVersionLocaleDetailsProperty(
                        source_bot_version="DRAFT",
                    ),
                ),
            ],
            description="AsyncResponseInterruptBot version",
        )

        self.interrupt_bot_alias = lex.CfnBotAlias(
            self,
            "InterruptBotAlias",
            bot_id=self.interrupt_bot.attr_id,
            bot_alias_name="live",
            bot_version=self.interrupt_bot_version.attr_bot_version,
            sentiment_analysis_settings={"DetectSentiment": False},
        )

        # --- Question Capture Bot ---
        self.question_capture_bot = lex.CfnBot(
            self,
            "QuestionCaptureBot",
            name="NexThinkQuestionCapture",
            role_arn=self.lex_bot_role.role_arn,
            data_privacy={"ChildDirected": False},
            idle_session_ttl_in_seconds=300,
            description=(
                "Lex bot for Option A — captures the caller's initial "
                "question via free-form speech input."
            ),
            auto_build_bot_locales=True,
            bot_locales=[
                lex.CfnBot.BotLocaleProperty(
                    locale_id="en_US",
                    nlu_confidence_threshold=0.40,
                    voice_settings=lex.CfnBot.VoiceSettingsProperty(
                        voice_id="Matthew",
                    ),
                    intents=[
                        # CaptureQuestion — captures free-form speech
                        lex.CfnBot.IntentProperty(
                            name="CaptureQuestion",
                            description="Captures the caller's question as free-form text.",
                            sample_utterances=[
                                lex.CfnBot.SampleUtteranceProperty(utterance="I have a question"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="I need help"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="help me"),
                                lex.CfnBot.SampleUtteranceProperty(utterance="I have an issue"),
                            ],
                            slot_priorities=[
                                lex.CfnBot.SlotPriorityProperty(
                                    priority=1,
                                    slot_name="question",
                                ),
                            ],
                            slots=[
                                lex.CfnBot.SlotProperty(
                                    name="question",
                                    description="The caller's question text.",
                                    slot_type_name="AMAZON.FreeFormInput",
                                    value_elicitation_setting=lex.CfnBot.SlotValueElicitationSettingProperty(
                                        slot_constraint="Required",
                                        prompt_specification=lex.CfnBot.PromptSpecificationProperty(
                                            message_groups_list=[
                                                lex.CfnBot.MessageGroupProperty(
                                                    message=lex.CfnBot.MessageProperty(
                                                        plain_text_message=lex.CfnBot.PlainTextMessageProperty(
                                                            value="Please describe your question or issue.",
                                                        ),
                                                    ),
                                                ),
                                            ],
                                            max_retries=2,
                                            allow_interrupt=True,
                                        ),
                                    ),
                                ),
                            ],
                        ),
                        # FallbackIntent (required)
                        lex.CfnBot.IntentProperty(
                            name="FallbackIntent",
                            description="Default fallback when no intent matches.",
                            parent_intent_signature="AMAZON.FallbackIntent",
                        ),
                    ],
                ),
            ],
        )

        self.question_capture_bot_version = lex.CfnBotVersion(
            self,
            "QuestionCaptureBotVersion",
            bot_id=self.question_capture_bot.attr_id,
            bot_version_locale_specification=[
                lex.CfnBotVersion.BotVersionLocaleSpecificationProperty(
                    locale_id="en_US",
                    bot_version_locale_details=lex.CfnBotVersion.BotVersionLocaleDetailsProperty(
                        source_bot_version="DRAFT",
                    ),
                ),
            ],
            description="NexThinkQuestionCapture version",
        )

        self.question_capture_bot_alias = lex.CfnBotAlias(
            self,
            "QuestionCaptureBotAlias",
            bot_id=self.question_capture_bot.attr_id,
            bot_alias_name="live",
            bot_version=self.question_capture_bot_version.attr_bot_version,
            sentiment_analysis_settings={"DetectSentiment": False},
        )

        # ---------------------------------------------------------------
        # SSM parameters for Lex bot aliases (wired from CDK outputs)
        # ---------------------------------------------------------------
        interrupt_bot_alias_param = ssm.StringParameter(
            self,
            "ParamLEX_BOT_ALIAS",
            parameter_name=f"{ssm_prefix}/lex-bot-alias",
            string_value=self.interrupt_bot_alias.attr_arn,
            description="Config: LEX_BOT_ALIAS — Interrupt bot alias ARN",
        )
        self.ssm_parameters["LEX_BOT_ALIAS"] = interrupt_bot_alias_param

        question_bot_alias_param = ssm.StringParameter(
            self,
            "ParamQUESTION_CAPTURE_BOT_ALIAS",
            parameter_name=f"{ssm_prefix}/question-capture-bot-alias",
            string_value=self.question_capture_bot_alias.attr_arn,
            description="Config: QUESTION_CAPTURE_BOT_ALIAS — Question capture bot alias ARN",
        )
        self.ssm_parameters["QUESTION_CAPTURE_BOT_ALIAS"] = question_bot_alias_param

        # Grant SSM read for the new Lex params to all Lambdas
        for fn in [self.callback_lambda, self.polling_lambda,
                   self.submit_lambda, self.disconnect_lambda]:
            interrupt_bot_alias_param.grant_read(fn)
            question_bot_alias_param.grant_read(fn)

        # ---------------------------------------------------------------
        # Connect Instance Associations (optional — requires instance ARN)
        # Pass via: cdk deploy -c connect_instance_arn=arn:aws:connect:...
        # ---------------------------------------------------------------
        if self._connect_instance_arn:
            # Associate Lex bots with the Connect instance
            connect.CfnIntegrationAssociation(
                self,
                "InterruptBotAssociation",
                instance_id=self._connect_instance_arn,
                integration_arn=self.interrupt_bot_alias.attr_arn,
                integration_type="LEX_BOT",
            )

            connect.CfnIntegrationAssociation(
                self,
                "QuestionCaptureBotAssociation",
                instance_id=self._connect_instance_arn,
                integration_arn=self.question_capture_bot_alias.attr_arn,
                integration_type="LEX_BOT",
            )

            # Associate Lambda functions with the Connect instance
            for name, fn in [
                ("PollingLambda", self.polling_lambda),
                ("SubmitLambda", self.submit_lambda),
                ("DisconnectLambda", self.disconnect_lambda),
            ]:
                connect.CfnIntegrationAssociation(
                    self,
                    f"{name}Association",
                    instance_id=self._connect_instance_arn,
                    integration_arn=fn.function_arn,
                    integration_type="LAMBDA_FUNCTION",
                )

            # --- Phone Number (optional — claims a DID number) ---
            # Pass via: cdk deploy -c connect_instance_arn=... -c claim_phone_number=true
            # Also accepts: -c phone_country_code=US  -c phone_type=DID
            if self.node.try_get_context("claim_phone_number") == "true":
                phone_country = self.node.try_get_context("phone_country_code") or "US"
                phone_type = self.node.try_get_context("phone_type") or "DID"

                self.phone_number = connect.CfnPhoneNumber(
                    self,
                    "PhoneNumber",
                    target_arn=self._connect_instance_arn,
                    country_code=phone_country,
                    type=phone_type,
                    description="Nexthink async multi-response IVR line",
                )

            # ---------------------------------------------------------------
            # Option E — Wisdom Assistant + Orchestration AI Agent
            # Pass via: cdk deploy -c connect_instance_arn=... -c deploy_orchestrator=true
            # ---------------------------------------------------------------
            if self.node.try_get_context("deploy_orchestrator") == "true":
                self._create_orchestrator_agent(ssm_prefix)

        # ---------------------------------------------------------------
        # Mock Nexthink Agent (optional — for prototyping without real Nexthink)
        # Pass via: cdk deploy -c deploy_mock_nexthink=true
        # ---------------------------------------------------------------
        if self.node.try_get_context("deploy_mock_nexthink") == "true":
            self._create_mock_nexthink(ssm_prefix)

    # -------------------------------------------------------------------
    # Option E — Wisdom Orchestrator AI Agent
    # -------------------------------------------------------------------

    def _create_orchestrator_agent(self, ssm_prefix: str) -> None:
        """Create the Q in Connect Assistant, AI Prompt, and Orchestration AI Agent.

        Resources created:
        - AgentCore Gateway (MCP server for tool invocations)
        - Lambda targets for submit-query and check-responses
        - Connect MCP server registration
        - Wisdom Assistant (domain)
        - AI Prompt (orchestration prompt from config/orchestrator_prompt.yaml)
        - AI Agent (ORCHESTRATION type with MCP tool configurations)
        - AI Agent Version (pinned version for deployment)
        """
        import pathlib

        from aws_cdk.aws_bedrock_agentcore_alpha import (
            Gateway,
            GatewayAuthorizer,
            ToolSchema,
        )

        # --- AgentCore Gateway (MCP server for tool invocations) ---
        # When integrating with Connect, the gateway must use the Connect
        # instance's OIDC discovery URL for authentication, and the JWT
        # `allowedAudience` must be the gateway's OWN ID (Connect sends the
        # gateway ID as the `aud` claim).
        #
        # A resource cannot reference its own attributes in CloudFormation —
        # `allowed_audience=[gateway.gateway_id]` renders as Fn::GetAtt on the
        # gateway itself and fails changeset creation with a circular-dependency
        # error. So: create the gateway with the correct discovery URL and a
        # placeholder audience, and let the McpServerRegistration custom
        # resource (which already runs after the gateway exists) call
        # UpdateGateway to set the real audience. See
        # lambdas/custom_resources/mcp_server_registration.py.
        connect_instance_url = self.node.try_get_context("connect_instance_url")
        if connect_instance_url and self._connect_instance_arn:
            self.agentcore_gateway = Gateway(
                self,
                "AgentCoreGateway",
                gateway_name="ConnectAsyncMcpGateway",
                authorizer_configuration=GatewayAuthorizer.using_custom_jwt(
                    discovery_url=f"{connect_instance_url}/.well-known/openid-configuration",
                    # Placeholder — replaced with the real gateway ID post-create.
                    allowed_audience=["pending-gateway-id"],
                ),
            )
        else:
            self.agentcore_gateway = Gateway(
                self,
                "AgentCoreGateway",
                gateway_name="ConnectAsyncMcpGateway",
            )

        # --- Submit Query Lambda target ---
        submit_schema = ToolSchema.from_local_asset("config/schemas/submit_query.json")
        self.submit_query_target = self.agentcore_gateway.add_lambda_target(
            "SubmitQueryTarget",
            gateway_target_name="submit-query",
            lambda_function=self.submit_lambda,
            tool_schema=submit_schema,
        )

        # --- Check Responses Lambda target ---
        check_schema = ToolSchema.from_local_asset("config/schemas/check_responses.json")
        self.check_responses_target = self.agentcore_gateway.add_lambda_target(
            "CheckResponsesTarget",
            gateway_target_name="check-responses",
            lambda_function=self.polling_lambda,
            tool_schema=check_schema,
        )

        # --- Fix IAM race condition ---
        # GatewayTarget creation validates Lambda invoke permissions immediately,
        # but the gateway service role's DefaultPolicy may not have propagated yet.
        # Add explicit dependency so targets wait for the policy to be created.
        gateway_role_policy = self.agentcore_gateway.node.find_child("ServiceRole").node.find_child("DefaultPolicy")
        if gateway_role_policy:
            self.submit_query_target.node.default_child.add_dependency(
                gateway_role_policy.node.default_child
            )
            self.check_responses_target.node.default_child.add_dependency(
                gateway_role_policy.node.default_child
            )

        # --- Register Gateway as MCP server on Connect instance ---
        # AWS::AppIntegrations::Application CFN resource does not expose
        # ApplicationType property, so we use a Custom Resource to call
        # the CreateApplication API with ApplicationType=MCP_SERVER.
        # Also associates the Wisdom assistant with the Connect instance
        # (WISDOM_ASSISTANT integration type not supported by CfnIntegrationAssociation).

        # --- Assistant domain (created before custom resource so ARN is available) ---
        self.wisdom_assistant = wisdom.CfnAssistant(
            self,
            "WisdomAssistant",
            name="NexthinkAsyncMultiResponse",
            type="AGENT",
            description="Q in Connect assistant for Nexthink async multi-response pattern",
            server_side_encryption_configuration=wisdom.CfnAssistant.ServerSideEncryptionConfigurationProperty(
                kms_key_id=self.kms_key.key_id,
            ),
        )

        if self._connect_instance_arn:
            from aws_cdk import CustomResource, custom_resources as cr

            mcp_reg_log_group = logs.LogGroup(
                self,
                "McpRegistrationLogGroup",
                log_group_name="/aws/lambda/ConnectAsync-McpRegistration",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            )

            mcp_registration_fn = lambda_.Function(
                self,
                "McpRegistrationFunction",
                function_name="ConnectAsync-McpRegistration",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="mcp_server_registration.handler",
                code=lambda_.Code.from_asset(
                    "lambdas/custom_resources",
                    bundling=BundlingOptions(
                        image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                        command=[
                            "bash", "-c",
                            "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
                        ],
                        local=_LocalBundler(),
                    ),
                ),
                timeout=Duration.seconds(120),
                memory_size=256,
                log_group=mcp_reg_log_group,
            )

            # Grant permissions for AppIntegrations APIs only.
            # Integration associations are created by CDK CfnIntegrationAssociation,
            # not by this Lambda (Lambda role can't update Connect SLR due to
            # permissions boundary in customer account).
            mcp_registration_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "app-integrations:CreateApplication",
                        "app-integrations:DeleteApplication",
                        "app-integrations:GetApplication",
                    ],
                    resources=["*"],
                )
            )
            # Gateway read + update: the handler sets allowedAudience to the
            # gateway's own ID post-create (see module docstring in the handler).
            mcp_registration_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock-agentcore:GetGateway",
                        "bedrock-agentcore:UpdateGateway",
                    ],
                    resources=[self.agentcore_gateway.gateway_arn],
                )
            )
            # UpdateGateway re-submits the gateway's roleArn → needs PassRole on it.
            mcp_registration_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["iam:PassRole"],
                    resources=[self.agentcore_gateway.role.role_arn],
                    conditions={
                        "StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}
                    },
                )
            )

            # Extract instance ID from ARN (last segment after /)
            instance_id = Fn.select(1, Fn.split("/", self._connect_instance_arn))

            mcp_provider = cr.Provider(
                self,
                "McpRegistrationProvider",
                on_event_handler=mcp_registration_fn,
            )

            self.mcp_server_registration = CustomResource(
                self,
                "McpServerRegistration",
                service_token=mcp_provider.service_token,
                properties={
                    "GatewayUrl": self.agentcore_gateway.gateway_url or "",
                    "GatewayArn": self.agentcore_gateway.gateway_arn,
                    "GatewayId": self.agentcore_gateway.gateway_id,
                    "ConnectInstanceId": instance_id,
                    "ApplicationName": "ConnectAsyncMcpServer",
                    "ApplicationNamespace": self.agentcore_gateway.gateway_id,
                    # Set on the gateway post-create (see handler). Empty in the
                    # non-Connect path → handler skips the audience update.
                    "DiscoveryUrl": (
                        f"{connect_instance_url}/.well-known/openid-configuration"
                        if connect_instance_url else ""
                    ),
                    "WisdomAssistantArn": self.wisdom_assistant.attr_assistant_arn,
                    "ForceUpdate": "v7",
                },
            )

        # Associate Wisdom assistant with the Connect instance.
        # CfnIntegrationAssociation does not support WISDOM_ASSISTANT type natively,
        # so this must be done manually after deploy:
        #   aws connect create-integration-association \
        #     --instance-id <ID> --integration-type WISDOM_ASSISTANT \
        #     --integration-arn <wisdom-assistant-arn>
        # The APPLICATION association for the MCP server app is also created
        # manually (or via post-deploy script) because CfnIntegrationAssociation
        # requires the app ARN which comes from the custom resource.
        # See DEPLOYMENT.md Step 5.

        # --- Load orchestrator prompt from file ---
        prompt_path = pathlib.Path(__file__).parent.parent / "config" / "orchestrator_prompt.yaml"
        import yaml as _yaml  # noqa: E402 — local import for build-time only

        with open(prompt_path, encoding="utf-8") as f:
            prompt_data = _yaml.safe_load(f)

        # Wisdom ORCHESTRATION (MESSAGES format) requires:
        # - system: <prompt text>
        # - messages: list with "{{$.conversationHistory}}" + optional assistant prefill
        # Build YAML manually to avoid quoting template variables.
        system_text = prompt_data.get("system", "")
        lines = ["system: |"]
        for line in system_text.splitlines():
            lines.append("  " + line)
        lines.append('messages:')
        lines.append('  - "{{$.conversationHistory}}"')
        lines.append('  - role: assistant')
        lines.append('    content: <message>')
        prompt_text = "\n".join(lines) + "\n"

        # --- AI Prompt (ORCHESTRATION type) ---
        self.orchestration_prompt = wisdom.CfnAIPrompt(
            self,
            "OrchestrationPrompt",
            assistant_id=self.wisdom_assistant.attr_assistant_id,
            name="NexthinkOrchestrationPrompt",
            type="ORCHESTRATION",
            api_format="MESSAGES",
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            template_type="TEXT",
            template_configuration=wisdom.CfnAIPrompt.AIPromptTemplateConfigurationProperty(
                text_full_ai_prompt_edit_template_configuration=wisdom.CfnAIPrompt.TextFullAIPromptEditTemplateConfigurationProperty(
                    text=prompt_text,
                ),
            ),
            description="Orchestration prompt for Nexthink async polling pattern",
        )

        # --- AI Agent (ORCHESTRATION with MCP tools) ---
        submit_tool_schema = {
            "type": "object",
            "properties": {
                "transcript": {
                    "type": "string",
                    "description": "The caller's transcribed question text.",
                },
                "sessionId": {
                    "type": "string",
                    "description": "Unique session identifier for this interaction.",
                },
                "callbackUrl": {
                    "type": "string",
                    "description": "HTTPS callback URL where Nexthink will deliver responses.",
                },
            },
            "required": ["transcript", "sessionId", "callbackUrl"],
        }

        check_tool_schema = {
            "type": "object",
            "properties": {
                "sessionId": {
                    "type": "string",
                    "description": "The session identifier from submit_query.",
                },
                "cursor": {
                    "type": "integer",
                    "description": "Last seen sequence number. Use 0 for the first poll.",
                },
            },
            "required": ["sessionId", "cursor"],
        }

        # --- AI Agent and Version are created MANUALLY after deploy ---
        # The OrchestrationAgent requires a WISDOM_ASSISTANT integration association
        # on the Connect instance, which can't be created by the Lambda custom resource
        # (permissions boundary blocks iam:PutRolePolicy on Connect SLR).
        # After deploy, run these manual steps:
        # 1. Create APPLICATION association: aws connect create-integration-association ...
        # 2. Create WISDOM_ASSISTANT association: aws connect create-integration-association ...
        # 3. Create AI Agent via scripts/configure_ai_agent.py or Connect console
        # See DEPLOYMENT.md for details.

        # --- SSM parameter for the assistant ARN ---
        assistant_arn_param = ssm.StringParameter(
            self,
            "ParamWISDOM_ASSISTANT_ARN",
            parameter_name=f"{ssm_prefix}/wisdom-assistant-arn",
            string_value=self.wisdom_assistant.attr_assistant_arn,
            description="Config: WISDOM_ASSISTANT_ARN — Q in Connect assistant ARN",
        )
        self.ssm_parameters["WISDOM_ASSISTANT_ARN"] = assistant_arn_param

        # --- SSM parameter for Gateway ARN ---
        gateway_arn_param = ssm.StringParameter(
            self,
            "ParamAGENTCORE_GATEWAY_ARN",
            parameter_name=f"{ssm_prefix}/agentcore-gateway-arn",
            string_value=self.agentcore_gateway.gateway_arn,
            description="Config: AGENTCORE_GATEWAY_ARN — AgentCore Gateway ARN",
        )
        self.ssm_parameters["AGENTCORE_GATEWAY_ARN"] = gateway_arn_param

    # -------------------------------------------------------------------
    # Mock Nexthink Agent (Bedrock-backed)
    # -------------------------------------------------------------------

    def _create_mock_nexthink(self, ssm_prefix: str) -> None:
        """Create the mock Nexthink agent Lambda + API Gateway endpoint.

        Simulates the Nexthink AI agent for prototyping. Receives queries,
        calls Bedrock to generate multi-part responses, and POSTs them
        back to the callback URL — feeding the shared callback ingestion layer.
        """
        mock_log_group = logs.LogGroup(
            self,
            "MockNexThinkLogGroup",
            log_group_name="/aws/lambda/ConnectAsync-MockNexThink",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Retrieve the callback API key from the existing secret
        mock_env = {
            "BEDROCK_MODEL_ID": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "RESPONSE_DELAY_S": "2",
            "MOCK_CALLBACK_API_KEY_SECRET_NAME": self.callback_api_key_secret.secret_name,
        }

        self.mock_nexthink_lambda = lambda_.Function(
            self,
            "MockNexThinkLambda",
            function_name="ConnectAsync-MockNexThink",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/mock_nexthink"),
            memory_size=512,
            timeout=Duration.seconds(60),
            environment=mock_env,
            log_group=mock_log_group,
        )

        # Bedrock invoke permission
        self.mock_nexthink_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                ],
            )
        )

        # Secrets Manager read for callback API key
        self.callback_api_key_secret.grant_read(self.mock_nexthink_lambda)

        # Self-invoke permission (for async callback delivery)
        self.mock_nexthink_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:ConnectAsync-MockNexThink"],
            )
        )

        # API Gateway for the mock endpoint
        self.mock_api = apigw.RestApi(
            self,
            "MockNexThinkApi",
            rest_api_name="ConnectAsyncMockNexThinkApi",
            description="Mock Nexthink agent endpoint for prototyping",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
            ),
        )

        mock_resource = self.mock_api.root.add_resource("agent")
        mock_integration = apigw.LambdaIntegration(self.mock_nexthink_lambda)
        mock_resource.add_method("POST", mock_integration)

        # Set the NEXTHINK_AGENT_URL SSM parameter to point to the mock
        ssm.StringParameter(
            self,
            "ParamMockNexThinkUrl",
            parameter_name=f"{ssm_prefix}/nexthink-agent-url",
            string_value=Fn.join("", [
                self.mock_api.url,
                "agent",
            ]),
            description="Config: NEXTHINK_AGENT_URL — Mock Nexthink agent endpoint",
        )
