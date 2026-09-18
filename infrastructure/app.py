#!/usr/bin/env python3
"""CDK app entry point for the Connect async multi-response stack."""

from aws_cdk import App, RemovalPolicy

from infrastructure.stack import ConnectAsyncMultiResponseStack

app = App()

ConnectAsyncMultiResponseStack(
    app,
    "ConnectAsyncMultiResponseStack",
    removal_policy=RemovalPolicy.DESTROY,
)

app.synth()
