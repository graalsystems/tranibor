# Sourced (`. /mnt/build-ca.sh`) at the start of every RUN that downloads over HTTPS, in a stage the
# published images do not ship (`docs/deployment.md`, "Building behind a TLS-inspecting proxy").
#
# A proxy that inspects TLS re-signs every certificate with its own authority, which no base image
# trusts: `uv`, `curl`, `git` and `pnpm` then refuse the download (`invalid peer certificate:
# UnknownIssuer`). The build context `build-ca` (empty by default) may hold that authority as one or
# more `*.pem` or `*.crt` files: this snippet appends them to the image's own bundle, for this RUN only,
# and points each tool at the result. Without a file it does nothing, and the build is what it was.
build_ca_found=""
for build_ca_file in /mnt/build-ca/*.pem /mnt/build-ca/*.crt; do
  [ -f "$build_ca_file" ] && build_ca_found="$build_ca_found $build_ca_file"
done
if [ -n "$build_ca_found" ]; then
  # The image's own bundle first, when it has one (`node:*-slim` has none: Node keeps its own roots).
  cat /etc/ssl/certs/ca-certificates.crt $build_ca_found > /tmp/build-ca.pem 2>/dev/null \
    || cat $build_ca_found > /tmp/build-ca.pem
  export SSL_CERT_FILE=/tmp/build-ca.pem CURL_CA_BUNDLE=/tmp/build-ca.pem GIT_SSL_CAINFO=/tmp/build-ca.pem \
    REQUESTS_CA_BUNDLE=/tmp/build-ca.pem NODE_EXTRA_CA_CERTS=/tmp/build-ca.pem
fi
