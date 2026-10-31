# Dockerfile for building and testing kiko-dev deb package
# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>

FROM debian:trixie-slim

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    debhelper \
    devscripts \
    dh-python \
    python3-all \
    python3-setuptools \
    python3-dev \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /build

# Copy source code
COPY . /build/kiko_lite/

# Build the deb package
WORKDIR /build/kiko_lite
RUN dpkg-buildpackage -us -uc -b

# Create test stage
FROM debian:trixie-slim as test

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    sqlite3 \
    ca-certificates \
    systemd \
    && rm -rf /var/lib/apt/lists/*

# Copy built deb package
COPY --from=0 /build/*.deb /tmp/

# Install the package
RUN dpkg -i /tmp/kiko-dev_*.deb

# Test import - only kikolib modules (not kiko_lite app modules)
RUN python3 -c "import kikolib; import kikolib.server; import kikolib.http_client; import kikolib.keystore; import kikolib.retry; import kikolib.tokens; import kikolib.sse; import kikolib.factory; import kikolib.yaml_lite; import kikolib.dotenv; print('All kikolib modules imported successfully')"

# Test CLI if available
RUN python3 -c "from kikolib import factory; print('Factory:', factory.report())"

# Test basic functionality
RUN python3 -c "\
import kikolib.server as server; \
import kikolib.http_client as http_client; \
import kikolib.retry as retry; \
app = server.App(); \
print('Server app created:', app); \
backend = http_client.client(); \
print('HTTP client created:', backend); \
policy = retry.RetryPolicy(); \
print('Retry policy created:', policy); \
print('All basic functionality tests passed!')"

# Cleanup
RUN apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/*.deb

CMD ["echo", "Build and test completed successfully!"]