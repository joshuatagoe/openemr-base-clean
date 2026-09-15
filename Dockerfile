# OpenEMR Flex provides Apache, PHP, Composer, Node, and OpenEMR's setup scripts.
FROM openemr/openemr:flex@sha256:1d2c8345a320cf2985c9d02138951267739ec498559ca724b3d5397e4568482e

# Package this fork inside the image so Railway does not use Railpack.
COPY --chown=apache:apache . /openemr

# Tell Flex to use the packaged repository instead of cloning OpenEMR.
ENV EASY_DEV_MODE_NEW=yes \
    DEVELOPER_TOOLS=no \
    XDEBUG_ON=0 \
    COMPOSER_ALLOW_SUPERUSER=1

EXPOSE 80
