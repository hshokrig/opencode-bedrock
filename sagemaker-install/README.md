# SageMaker offline install

This directory contains a prebuilt Linux x64 release. Build work is already complete. SageMaker
does not need public internet access, pip, uv, Bun, npm, or a package registry for the normal
installation.

The archive only runs on Linux x64 (`uname -m` reports `x86_64`) with Python 3.10 or newer.

## 1. Copy the release to Bitbucket

The release currently lives on the GitHub branch `offline-release`. On the Windows workstation,
clone that branch and push it to the approved Bitbucket repository. Do not unpack, recompress, or
rename the four release files.

```powershell
git clone --branch offline-release --single-branch https://github.com/hshokrig/opencode-bedrock.git
cd opencode-bedrock
git remote add bitbucket <BITBUCKET_REPOSITORY_URL>
git push -u bitbucket offline-release
```

If the Bitbucket remote already exists, skip the `git remote add` command.

## 2. Clone the repository in SageMaker

```bash
git clone --branch offline-release --single-branch \
  <BITBUCKET_REPOSITORY_URL> opencode-bedrock
cd opencode-bedrock/sagemaker-install
```

The checksum files contain relative filenames, so run the remaining install commands from this
directory.

## 3. Check the SageMaker image

```bash
uname -m
python3 --version
command -v git
command -v bwrap
```

`uname -m` must print `x86_64`, and Python must be version 3.10 or newer. Git and bubblewrap
(`bwrap`) must already be supplied by the SageMaker image or an administrator-managed image layer.
Bubblewrap is an operating-system package and cannot be installed with uv.

## 4. Verify and install the release

```bash
sha256sum -c install-opencode-bedrock.sh.sha256
sha256sum -c opencode-bedrock-*.tar.gz.sha256

chmod +x install-opencode-bedrock.sh
./install-opencode-bedrock.sh opencode-bedrock-*.tar.gz

export PATH="$HOME/.local/bin:$PATH"
opencode-bedrock doctor
```

Both checksum commands must report `OK`. The installer also verifies every file inside the archive
before updating the command links under `$HOME/.local/bin`.

For an independent comparison, the expected SHA-256 values for this release are:

```text
3320e8cf3ef5209c43df73399cbb1f0f6d4bc3dc2b8fa0ab8c7bfae5e4b96b14  install-opencode-bedrock.sh
20dc7bdb19d6e4069576401787687fefb4a4294ab47c385f383bcc840ba700ff  opencode-bedrock-0.1.0+6daafcffd396-linux-x64.tar.gz
```

Keep a copy of those expected values in a trusted channel separate from the transferred files.

## 5. Configure SageMaker

Set the Bedrock region and inference profile. Do not save account IDs, profile ARNs, credentials,
or private endpoints in the repository.

```bash
export AWS_REGION='<SOURCE_REGION>'
export BEDROCK_INFERENCE_PROFILE='<PROFILE_ID_OR_ARN>'
```

Before starting a service, put its state on an approved persistent local or block-backed path that
supports SQLite WAL locking. Do not use an unverified EFS or NFS path.

```bash
export XDG_STATE_HOME='<PERSISTENT_LOCAL_OR_BLOCK_PATH>/opencode-bedrock-state'
mkdir -p "$XDG_STATE_HOME"
```

Run `opencode-bedrock doctor` again after setting the environment. A successful result confirms the
local executable and bubblewrap setup. It does not make an AWS call.

## 6. Run the optional AWS smoke test

The AWS smoke test calls Bedrock and may incur charges:

```bash
RUN_AWS_SMOKE=1 opencode-bedrock-verify-aws
```

This optional script needs Boto3. SageMaker images normally include it. If Boto3 is missing, create
an isolated uv environment using the approved Artifactory configuration, activate it, and rerun the
test:

```bash
uv venv --python python3 "$HOME/.venvs/opencode-bedrock-validation"
source "$HOME/.venvs/opencode-bedrock-validation/bin/activate"
uv pip install boto3
RUN_AWS_SMOKE=1 opencode-bedrock-verify-aws
```

`uv sync` is not part of the application installation and will not add Boto3 because Boto3 is not
a runtime dependency of this project.

## 7. Register and start a workspace

Use an existing Git repository on the persistent workspace volume:

```bash
opencode-bedrock project add --name sample --path /absolute/path/to/sample-repo
opencode-bedrock project list
opencode-bedrock start --project sample
opencode-bedrock status
```

Useful follow-up commands are:

```bash
opencode-bedrock logs --project sample --follow
opencode-bedrock attach --project sample
opencode-bedrock stop --project sample
```

If `doctor` reports that bubblewrap is missing or unusable, stop there. The SageMaker base image or
its administrator-managed layer must be fixed before the service can run safely.
