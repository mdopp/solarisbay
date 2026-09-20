# PI WEB — the web UI for the Pi Coding Agent (#1357).
#
# Upstream (jmfederico/pi-web) publishes an **npm package only**: its `docker/`
# tree is a Dockerfile you build yourself, and no OCI image is pushed anywhere.
# So this repo builds one, like it does for the engine and the gatekeeper, and
# `templates/pi-web` pulls `ghcr.io/mdopp/solaris-pi-web:latest`.
#
# Two stages for the same reason upstream splits them: node-pty is a native
# module and needs a compiler, which has no business in the image that ends up
# on the box.

ARG NODE_IMAGE=docker.io/library/node:22-trixie

FROM ${NODE_IMAGE} AS package

# Pinned, not `latest`: a template redeploy must not silently move the agent
# runtime under a running session. Bump it here, in one commit, on purpose.
ARG PI_WEB_VERSION=1.202609.0

ENV NPM_CONFIG_UPDATE_NOTIFIER=false

# `--include=peer` pulls @earendil-works/pi-coding-agent, which is a peer
# dependency rather than a dependency: without it pi-web installs but has no
# agent to run. npm here runs install scripts by default, so node-pty builds
# itself — upstream's `--allow-scripts=node-pty` is an npm 12 flag with no
# equivalent on this npm and would abort the install.
RUN npm install -g --omit=dev --include=peer --no-audit --no-fund \
      "@jmfederico/pi-web@${PI_WEB_VERSION}" \
 && ln -sf "$(npm root -g)/@jmfederico/pi-web/node_modules/.bin/pi" \
           "$(npm prefix -g)/bin/pi" \
 && npm cache clean --force

FROM docker.io/library/node:22-trixie-slim AS runtime

# git and a real shell are the point of the product — an agent session is a
# shell in a repository. tini reaps the processes those sessions leave behind.
# python3 runs `pi-web-project` below: the box's other post-deploy/CLI tooling is
# stdlib Python and covered by the repo's ruff + pytest gates, which a second
# language in this one file would leave untested.
#
# The second install line is the session's own toolbox (#1439). The image shipped
# without it while the assist catalog a session is handed prescribes exactly these
# commands: `curl` appears in five assists -- among them
# `footgun-mcp-from-a-container-on-the-box`, which explains how to reach
# ServiceBay's API from a container on the box, and this IS that container -- and
# `make` in sixteen. A session that cannot run what its own instructions prescribe
# does not fail loudly; it routes around them in silence, and the gap survives
# because nobody sees it. Still no compiler: that is what the `package` stage is
# for.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git less procps python3 ripgrep tini \
      curl wget jq make patch file unzip zip openssh-client \
 && rm -rf /var/lib/apt/lists/*

COPY --from=package /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=package /usr/local/bin /usr/local/bin

# `gh` is how a session does GitHub work -- open a repo, a PR, read a CI log --
# and Debian's archive carries no usable version, so it comes from the project's
# own release as one static binary. Pinned for the same reason PI_WEB_VERSION is:
# a rebuild must not move a tool under a running session. linux/amd64 only,
# matching build-images.yml; a second architecture needs this line too.
ARG GH_VERSION=2.101.0
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
      -o /tmp/gh.tgz \
 && tar -xzf /tmp/gh.tgz -C /tmp \
 && install -m 0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh \
 && rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_amd64"

# Pi ships no MCP, so a session reads this box through a CLI instead: one
# delegated read-only ServiceBay token per project (#1395).
COPY --chmod=0755 pi_web_project.py /usr/local/bin/pi-web-project

# The ServiceBay agent kit (servicebay#2903) is mounted read-only at
# /opt/servicebay: `servicebay` puts its CLI on PATH with the right token file,
# `pi-web-agent-kit` turns its assists into Pi skills and its AGENTS.md into
# Pi's global context file. Neither ships a copy of anything from the kit — the
# kit is delivered from mdopp/servicebay and refreshed on the box hourly.
COPY --chmod=0755 pi_servicebay.py /usr/local/bin/servicebay
COPY --chmod=0755 pi_agent_kit.py /usr/local/bin/pi-web-agent-kit

# The Pi autoloop (#1398 slice B): a third process in the pod that works issues
# labelled `pi:ready` headlessly. Same reason as above for the language — it is
# stdlib Python, so the repo's ruff + pytest gates cover it.
COPY --chmod=0755 pi_autoloop.py /usr/local/bin/pi-web-autoloop

# The model gate (#1435): the OpenAI-compatible door on the pod's own loopback
# that every session and the autoloop talk to. It forwards to the policy proxy
# on LLAMA_PORT and, when the standing mode refuses the wanted preset, asks the
# box for the mode that permits it over a file the host broker watches — this
# pod can reach neither systemd nor the Engine's loopback lease API.
COPY --chmod=0755 pi_model_gate.py /usr/local/bin/pi-web-model-gate

# The Pi extensions this pod installs globally into the agent directory (#1435).
# They ride in the image for the same reason the plugin below does: they are
# written against the Pi extension API of the PI_WEB_VERSION pinned above. An
# init container copies them into $PI_CODING_AGENT_DIR/extensions, which is where
# Pi discovers global extensions — and since providers are captured when sessiond
# starts, that copy is in place before the daemon that reads it.
COPY extensions /opt/solaris/pi-extensions

# The "Repo klonen" plugin (#1395 slice B). It rides in the image rather than in
# the template's asset tree because it is written against the plugin API of the
# PI_WEB_VERSION pinned above — the two move together or not at all. An init
# container copies it to $PI_WEB_DATA_DIR/plugins, which is the local plugin
# root PI WEB scans; a plugin found there is enabled unless the config says
# otherwise, so there is nothing to switch on in Settings.
COPY plugins /opt/solaris/pi-web-plugins

# Everything durable lives under /data, which the pod mounts from the host:
# the PI WEB state (project registry, session archives, the sessiond socket)
# and the Pi agent directory that carries auth, settings and models.json —
# the file the post-deploy writes to point Pi at llama-server on this box.
ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/data/home \
    XDG_CONFIG_HOME=/data/config \
    PI_WEB_HOST=0.0.0.0 \
    PI_WEB_PORT=8504 \
    PI_WEB_DATA_DIR=/data/pi-web \
    PI_WEB_SESSIOND_SOCKET=/data/pi-web/sessiond.sock \
    PI_CODING_AGENT_DIR=/data/pi-agent \
    SHELL=/bin/bash \
    TERM=xterm-256color

WORKDIR /workspace
USER node

EXPOSE 8504

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pi-web-server"]
