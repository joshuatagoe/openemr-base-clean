# OpenEMR Flex provides Apache, PHP, Composer, Node, and OpenEMR's setup scripts.
FROM openemr/openemr:flex@sha256:1d2c8345a320cf2985c9d02138951267739ec498559ca724b3d5397e4568482e

# Package this fork inside the image so Railway does not use Railpack.
COPY --chown=apache:apache . /openemr

# Tell Flex to use the packaged repository instead of cloning OpenEMR.
ENV EASY_DEV_MODE_NEW=yes \
    DEVELOPER_TOOLS=no \
    XDEBUG_ON=0 \
    COMPOSER_ALLOW_SUPERUSER=1

# Prebuild PHP and JavaScript dependencies at image-build time so the Railway
# runtime container never runs Composer, npm, or Webpack (which OOMs on Node's
# default heap). /couchdb/data is created up front because the Easy Development
# startup script rsyncs it and aborts the container when it is missing.
RUN mkdir -p /couchdb/data \
    && cd /openemr \
    && COMPOSER_ALLOW_SUPERUSER=1 composer install --no-dev --no-interaction --prefer-dist \
    && npm install \
    && NODE_OPTIONS=--max-old-space-size=2048 npm run build \
    && composer dump-autoload -o

# Patient dashboard (React), modes B/C: build the SMART web app into the Co-Pilot
# module's public folder, where Apache serves it as static files under the
# folder's own .htaccess (CSP). Uses the Flex image's Node (24; the dashboard
# needs >= 22). The folder is gitignored and excluded from the build context, so
# the image never ships a stale local build; node_modules and this step's own npm
# cache (a throwaway folder, ~55 MB otherwise left in the layer) are removed afterwards.
RUN cd /openemr/dashboard \
    && npm ci --no-audit --no-fund --cache /tmp/dashboard-npm-cache \
    && npm run build:smart \
    && rm -rf node_modules web/node_modules bff/node_modules /tmp/dashboard-npm-cache \
    && chown -R apache:apache /openemr/interface/modules/custom_modules/oe-module-copilot/public/dashboard

# Skip the inherited Flex build steps on container start; everything is prebuilt above.
ENV FORCE_NO_BUILD_MODE=yes

# Start through the Co-Pilot start script (ADR-009 section 6): openemr.sh runs its
# setup with FLEX_SKIP_APACHE_EXEC=yes and returns, the module's migration runner
# applies the module schema as the web user (never blocking), then Apache is exec'd.
# CRs are stripped in case the build context came from a Windows checkout.
RUN sed 's/\r$//' /openemr/interface/modules/custom_modules/oe-module-copilot/bin/copilot-start.sh \
        > /usr/local/bin/copilot-start.sh \
    && chmod 755 /usr/local/bin/copilot-start.sh
CMD ["/bin/sh", "/usr/local/bin/copilot-start.sh"]

EXPOSE 80
