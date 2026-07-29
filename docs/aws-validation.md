# AWS validation checklist

These checks make real AWS calls and may incur Bedrock charges. Run them inside SageMaker after setting:

```bash
export AWS_REGION='your-source-region'
export BEDROCK_INFERENCE_PROFILE='your-profile-id-or-arn'
export PATH="$HOME/.local/bin:$PATH"
```

## 1. Caller identity

```bash
aws sts get-caller-identity
```

Confirm that the ARN belongs to the intended SageMaker execution role.

## 2. Inference-profile visibility

```bash
aws bedrock get-inference-profile \
  --region "$AWS_REGION" \
  --inference-profile-identifier "$BEDROCK_INFERENCE_PROFILE"
```

Check that `status` is `ACTIVE` and review every model ARN in `models`.

## 3. Invocation permission

```bash
aws bedrock-runtime converse \
  --region "$AWS_REGION" \
  --model-id "$BEDROCK_INFERENCE_PROFILE" \
  --messages '[{"role":"user","content":[{"text":"Reply with exactly: bedrock-ok"}]}]' \
  --inference-config '{"maxTokens":32,"temperature":0}'
```

This confirms `bedrock:InvokeModel`.

## 4. Streaming invocation

The AWS CLI does not support Bedrock streaming operations. SageMaker images normally include Boto3.

```bash
RUN_AWS_SMOKE=1 opencode-bedrock-verify-aws
```

The script repeats caller, profile, and Converse checks, then uses `converse_stream`. This confirms `bedrock:InvokeModelWithResponseStream`.

## 5. OpenCode connection to the Opus profile

```bash
opencode-bedrock doctor
opencode-bedrock start --workspace /absolute/path/to/sample-repo
opencode-bedrock task --workspace /absolute/path/to/sample-repo \
  "Reply with the active workspace path and the selected model provider. Do not edit files."
opencode-bedrock logs --workspace /absolute/path/to/sample-repo --follow
```

Stop following after the response arrives with `Ctrl-C`; the service continues.

## 6. Read inside the sample repository

```bash
opencode-bedrock task --workspace /absolute/path/to/sample-repo \
  "Read README.md and report its first heading. Do not edit files."
```

## 7. Reject an out-of-workspace read

```bash
opencode-bedrock task --workspace /absolute/path/to/sample-repo \
  "Try to read /etc/passwd with the read tool. Report the exact denial and do nothing else."
```

The task must report a denial. Treat any file content as a security failure and stop the service.

## 8. Run a safe command in the repository

```bash
opencode-bedrock task --workspace /absolute/path/to/sample-repo \
  "Run pwd and git status --short. Report both outputs. Do not edit files."
```

`pwd` must equal the selected repository path.

## 9. Start, detach, inspect, attach, and stop

```bash
opencode-bedrock start --project my-project
opencode-bedrock status
opencode-bedrock attach --project my-project
# Leave the client, then:
opencode-bedrock status
opencode-bedrock stop --project my-project
```

## 10. Terminal-disconnect persistence

Start the service, note its PID, and close the SageMaker terminal. Open a new terminal in the same running application:

```bash
opencode-bedrock status
opencode-bedrock attach --project my-project
```

The PID should still be running and the earlier session should be available.

## 11. SageMaker application restart

Start a service and stop the SageMaker application from the console. Start the application again on the same persistent home volume:

```bash
opencode-bedrock status
opencode-bedrock start --project my-project
opencode-bedrock attach --project my-project
```

The old process must be gone. `status` may show stale state until `start` replaces it. Session history is expected only when the XDG state directory survived on persistent storage.

Record the SageMaker image, application type, Region, profile ARN, caller identity ARN, VPC endpoint configuration, and result of each check. Those results are intentionally absent from this repository.
