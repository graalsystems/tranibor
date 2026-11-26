# Extra certificate authorities for the build

Empty on purpose. `compose.yaml` hands this directory to the image builds as the `build-ca` context
unless `TRANIBOR_BUILD_CA_DIR` names another one. Put nothing here: point `TRANIBOR_BUILD_CA_DIR` at a
directory of your own that holds your proxy's authority as a `*.pem` or `*.crt` file. See
`docs/deployment.md`, "Building behind a TLS-inspecting proxy".
