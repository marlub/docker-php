# Optional local selection: --var 'EXTENSIONS=bcmath redis'. Pins stay per PHP minor.
variable "EXTENSIONS" {
  type    = string
  default = null
}

variable "EXTERNAL_EXTENSIONS" {
  default = ["redis", "amqp", "xdebug", "pcov", "xhprof", "opentelemetry", "grpc", "protobuf"]
}

# PHPIZE_DEPS comes from the PHP base image; these are the additional build tools.
variable "BUILD_PACKAGES" {
  default = [
    "automake", "libtool", "unzip", "linux-headers", "git", "freetype-dev",
    "libjpeg-turbo-dev", "libpng-dev", "libxpm-dev", "libwebp-dev", "libavif-dev",
    "icu-dev", "openldap-dev", "libzip-dev", "postgresql-dev", "rabbitmq-c-dev",
    "liblzf-dev", "zstd-dev", "lz4-dev", "jq"
  ]
}

variable "RUNTIME_PACKAGES" {
  default = [
    "brotli-libs", "freetype", "libjpeg-turbo", "libpng", "libxpm", "libwebp",
    "libavif", "icu-libs", "icu-data-full", "libldap", "libzip", "libpq",
    "rabbitmq-c", "liblzf", "zstd-libs", "lz4-libs", "libstdc++", "libgcc"
  ]
}

function "extension_args" {
  params = [config]
  result = merge(config, {
    EXTENSIONS = EXTENSIONS != null ? EXTENSIONS : config.EXTENSIONS
    BUNDLED_EXTENSIONS = join(" ", [
      for name in split(" ", EXTENSIONS != null ? EXTENSIONS : config.EXTENSIONS) : name
      if name != "" && !contains(EXTERNAL_EXTENSIONS, name)
    ])
    INSTALL_RUNTIME_PACKAGES = trimspace(EXTENSIONS != null ? EXTENSIONS : config.EXTENSIONS) != "" || config.PACKAGES != "" ? "true" : "false"
  }, {
    for name in EXTERNAL_EXTENSIONS : "BUILD_${upper(name)}" => contains(
      split(" ", EXTENSIONS != null ? EXTENSIONS : config.EXTENSIONS), name
    ) ? "true" : "false"
  })
}

target "_common" {
  context    = "."
  dockerfile = "Dockerfile"
  extra-hosts = { "pecl.php.net" = "127.0.0.1" }
  attest = ["type=provenance,disabled=true"]
  args = {
    BUILD_PACKAGES   = join(" ", BUILD_PACKAGES)
    RUNTIME_PACKAGES = join(" ", RUNTIME_PACKAGES)
  }
}
