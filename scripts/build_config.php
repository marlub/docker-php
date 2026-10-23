<?php
// Turn the resolved snapshot into safely quoted build instructions. Standalone
// Docker builds use the checked-in, checksum-pinned defaults instead.
declare(strict_types=1);

$supplied = getenv('DEPENDENCIES') ?: '';
$config = json_decode($supplied ?: file_get_contents(__DIR__ . '/build-defaults.json'), true, 512, JSON_THROW_ON_ERROR);
$extensions = preg_split('/\s+/', trim(getenv('EXTENSIONS') ?: ''), -1, PREG_SPLIT_NO_EMPTY);
$core = [];
$external = [];
$commands = [];
$xhprof = false;

function variable(string $name, string $value): void {
    echo $name . '=' . escapeshellarg($value) . "\n";
}

function tool(array $data, string $prefix): void {
    if (!preg_match('/^[a-f0-9]{64}$/D', $data['sha256'] ?? '') || !str_starts_with($data['url'] ?? '', 'https://')) {
        throw new RuntimeException("Missing URL or checksum for $prefix");
    }
    variable($prefix . '_URL', $data['url']);
    variable($prefix . '_SHA256', $data['sha256']);
}

foreach ($extensions as $name) {
    if (!preg_match('/^[a-z][a-z0-9_]*$/D', $name)) {
        throw new RuntimeException("Invalid extension name: $name");
    }
    if ($name === 'opcache' && PHP_VERSION_ID >= 80500) {
        throw new RuntimeException('PHP 8.5+ opcache is built in; remove it from EXTENSIONS');
    }
    if (isset($config['pie_packages'][$name])) {
        $package = $config['pie_packages'][$name];
        if (!preg_match('/^[a-z0-9_.-]+\/[a-z0-9_.-]+$/D', $package['package']) ||
            !preg_match('/^\d+(?:\.\d+){1,3}$/D', $package['version'])) {
            throw new RuntimeException("Invalid PIE pin for $name");
        }
        $args = [$package['package'] . ':' . $package['version']];
        foreach ($package['configure_options'] as $option) {
            if (!preg_match('/^--[a-z0-9-]+(?:=[a-z0-9\/.+-]+)?$/D', $option)) {
                throw new RuntimeException("Invalid configure option for $name");
            }
            $args[] = $option;
        }
        $commands[] = 'php /tmp/php-build/pie.phar --no-interaction --no-ansi install --no-cache --skip-enable-extension --make-parallel-jobs="${PHP_BUILD_JOBS:-1}" ' .
            implode(' ', array_map('escapeshellarg', $args));
        $commands[] = 'docker-php-ext-enable ' . escapeshellarg($name);
        $external[] = $name;
    } elseif ($name === 'xhprof') {
        $xhprof = true;
        tool($config['xhprof'], 'XHPROF');
    } else {
        $core[] = $name;
    }
}
if ($supplied && implode(' ', $core) !== $config['extensions_install']) {
    throw new RuntimeException('EXTENSIONS does not match the resolved bundled extension list');
}
variable('BUILD_CORE', implode(' ', $core));
variable('BUILD_EXTERNAL', implode(' ', $external));
variable('BUILD_XHPROF', $xhprof ? '1' : '0');
if ($extensions) {
    tool($config['composer'], 'COMPOSER');
}
if ($external) {
    tool($config['pie'], 'PIE');
}
file_put_contents(__DIR__ . '/install-pie.sh', "#!/bin/sh\nset -eu\n" . implode("\n", $commands) . "\n");
