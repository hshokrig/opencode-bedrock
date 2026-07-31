# SageMaker setup

Install the artifact on persistent SageMaker storage when sessions and service logs must remain after an application restart. The process itself stops when SageMaker terminates the application or compute instance. The live `XDG_STATE_HOME` must be on a local or block-backed filesystem compatible with SQLite WAL, not an unverified EFS/NFS home mount.

Set the two required runtime values:

```bash
export AWS_REGION='your-source-region'
export BEDROCK_INFERENCE_PROFILE='your-profile-id-or-arn'
export PATH="$HOME/.local/bin:$PATH"
```

Do not put these values in the repository. Use the SageMaker environment or a protected user configuration outside project workspaces.

The wrapper passes AWS default-chain settings to OpenCode. In SageMaker, the execution role supplies temporary credentials. No long-term access key is required.

Install bubblewrap through the base image or an administrator-managed image layer. `opencode-bedrock doctor` must report that its user namespace test succeeds. A present but unusable binary is not enough.

If Bedrock is reached through a VPC endpoint, set:

```bash
export BEDROCK_RUNTIME_ENDPOINT='https://your-approved-runtime-endpoint'
export BEDROCK_CONTROL_ENDPOINT='https://your-approved-control-endpoint'
```

`BEDROCK_RUNTIME_ENDPOINT` configures OpenCode and the invocation smoke test. `BEDROCK_CONTROL_ENDPOINT` is used only by the profile-visibility smoke test. Omit either value when the standard regional endpoint is reachable.

Private subnets may also need an STS VPC endpoint for caller/credential validation and an S3 VPC
endpoint for artifact transfer. If the S3 object uses SSE-KMS, include the reviewed KMS decrypt
permission. Confirm private DNS, endpoint security groups, endpoint policies, and any custom CA
bundle before testing. The runtime endpoint must remain an HTTPS AWS endpoint; redirects are
rejected.

Apply a reviewed copy of [the IAM template](../policies/sagemaker-bedrock-iam.json) to the SageMaker execution role. Replace every placeholder. For a cross-Region profile, list the foundation model ARN in the source Region and every current destination Region. AWS rejects the request if an SCP blocks any required destination.

Run [the AWS validation checklist](aws-validation.md) after installation.

SageMaker does not need GitHub or registry access and should not build from the repository. Build
and test the commit-specific archive on the connected Linux machine, then transfer the four release
files through the approved upload or S3 path.
