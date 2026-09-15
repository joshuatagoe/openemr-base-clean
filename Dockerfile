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

# Skip the inherited Flex build steps on container start; everything is prebuilt above.
ENV FORCE_NO_BUILD_MODE=yes

EXPOSE 80
