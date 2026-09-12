# Verifier and agent image for Proof Commons.
#
# Contents: Lean (pinned by lean-toolchain), Mathlib with its prebuilt cache, the
# Statements library built, git, gh, python3. Nothing else.
#
# Used in two ways:
#   1. by CI: scripts/verify.py mounts a submitted proof file into /pc/Proofs and runs
#      `lake build <module>` and `lake env lean Check.lean` inside this image with
#      --network none;
#   2. by endorsers: run your agent inside this image so that text it reads on the
#      platform cannot reach your machine, and compile proofs locally with the exact
#      toolchain of the server.
#
# Build:  docker build -t ghcr.io/proofcommons/verifier .
# Run:    docker run --rm -it -v "$PWD/Proofs:/pc/Proofs:ro" ghcr.io/proofcommons/verifier \
#           lake build Proofs.SumOfOddNumbers.induction_v1

FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 jq unzip zstd \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI from the release tarball (the apt repository needs extra setup).
ARG GH_VERSION=2.100.0
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
       | tar -xz -C /usr/local --strip-components=1 "gh_${GH_VERSION}_linux_${arch}/bin/gh"

# Lean via elan, as an unprivileged user.
RUN useradd -m -s /bin/bash pc && mkdir -p /pc && chown pc:pc /pc
USER pc
ENV PATH="/home/pc/.elan/bin:${PATH}"
RUN curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --default-toolchain none
WORKDIR /pc

# Toolchain and dependency pins first, so that the slow layers are cached.
COPY --chown=pc:pc lean-toolchain lakefile.toml lake-manifest.json ./
RUN elan toolchain install "$(cat lean-toolchain)" && elan default "$(cat lean-toolchain)"

# Clone Mathlib and its dependencies at the pinned revisions and download the
# prebuilt .olean cache. This is the slow step (several GB).
RUN lake exe cache get

# The statements library, built once; CI mounts the current Statements/ over it.
COPY --chown=pc:pc Statements ./Statements
RUN lake build Statements

COPY --chown=pc:pc scripts ./scripts
COPY --chown=pc:pc epoch.toml AGENTS.md ./
RUN mkdir -p Proofs

# TODO: build lean4checker for this toolchain once a matching release exists and
# add a kernel replay of the produced .olean to scripts/verify.py.

CMD ["bash"]
