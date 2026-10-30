<?php
declare(strict_types=1);

function commandOutput(array $arguments): string {
    exec(implode(' ', array_map('escapeshellarg', $arguments)), $lines, $status);
    if ($status !== 0) throw new RuntimeException('Command failed: ' . $arguments[0]);
    return implode("\n", $lines);
}

function inventory(?string $output = null): array {
    $output ??= commandOutput(['dpkg-query', '-W', '-f=${Package}\t${Version}\t${db:Status-Status}\n']);
    $packages = [];
    foreach (explode("\n", trim($output)) as $line) {
        $fields = explode("\t", $line);
        if (count($fields) !== 3) throw new RuntimeException('Malformed DPKG inventory');
        if ($fields[2] === 'installed') $packages[$fields[0]] = $fields[1];
    }
    ksort($packages);
    return $packages;
}

function release(): string {
    $release = parse_ini_file('/etc/os-release');
    if (($release['ID'] ?? '') !== 'debian') throw new RuntimeException('Expected Debian');
    return $release['VERSION_CODENAME'];
}

function readLock(string $path): array {
    $lock = json_decode(file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
    if (($lock['debian'] ?? null) !== release() || ($lock['architecture'] ?? null) !== commandOutput(['dpkg', '--print-architecture'])) {
        throw new RuntimeException('APT lock does not match the base release/architecture');
    }
    if (!is_array($lock['packages'] ?? null)) throw new RuntimeException('Missing APT package pins');
    foreach ($lock['packages'] as $name => $version) {
        if (!preg_match('/^[a-z0-9][a-z0-9+.-]+$/D', $name) || !is_string($version)
            || !preg_match('/^[0-9][a-zA-Z0-9.+:~\-]*$/D', $version)) {
            throw new RuntimeException('Invalid APT package pin');
        }
    }
    return $lock;
}

function checkBaseVersions(array $baseline, array $pins): void {
    foreach ($pins as $name => $version) {
        if (!isset($baseline[$name])) continue;
        exec('dpkg --compare-versions ' . escapeshellarg($baseline[$name]) . ' gt ' . escapeshellarg($version), $unused, $status);
        if ($status === 0) throw new RuntimeException("APT lock would downgrade base package: $name");
        if ($status !== 1) throw new RuntimeException('Invalid Debian version comparison');
    }
}

function verifyInventory(array $current, array $baseline, array $pins, array $required): void {
    foreach ($current as $name => $version) {
        if ($version !== ($pins[$name] ?? null) && $version !== ($baseline[$name] ?? null)) {
            throw new RuntimeException("Untracked APT package or version: $name=$version");
        }
    }
    foreach ($baseline as $name => $version) {
        if (!isset($current[$name])) throw new RuntimeException("Base package removed: $name");
    }
    foreach ($required as $name) {
        if (!isset($current[$name]) || $current[$name] !== ($pins[$name] ?? $baseline[$name] ?? null)) {
            throw new RuntimeException("Missing APT pin: $name");
        }
    }
}

function installLocked(string $lockPath, string $recordPath, array $requested): void {
    $lock = readLock($lockPath);
    $current = inventory();
    $record = is_file($recordPath)
        ? json_decode(file_get_contents($recordPath), true, 512, JSON_THROW_ON_ERROR)
        : ['baseline' => $current, 'required' => []];
    checkBaseVersions($current, $lock['packages']);
    $arguments = [];
    foreach ($requested as $name) {
        $version = $lock['packages'][$name] ?? $current[$name] ?? null;
        if ($version === null) throw new RuntimeException("Package not covered by APT lock: $name");
        $arguments[] = "$name=$version";
    }
    if ($arguments) {
        // Pin the complete allowed inventory so APT resolves transitive dependencies
        // at committed versions, including inherited packages that must stay put.
        $preferences = '';
        foreach (array_replace($current, $lock['packages']) as $name => $version) {
            $preferences .= "Package: $name\nPin: version $version\nPin-Priority: 1001\n\n";
        }
        $path = '/etc/apt/preferences.d/php-runtime-lock';
        file_put_contents($path, $preferences);
        try {
            commandOutput(['apt-get', 'update']);
            commandOutput(['apt-get', 'install', '-y', '--no-install-recommends', '--no-remove', ...$arguments]);
        } finally {
            unlink($path);
        }
    }
    $current = inventory();
    // Every package changed or added by APT becomes mandatory for later checks.
    $changed = array_keys(array_diff_assoc($current, $record['baseline']));
    $record['required'] = array_values(array_unique([...$record['required'], ...$requested, ...$changed]));
    verifyInventory($current, $record['baseline'], $lock['packages'], $record['required']);
    if (!is_dir(dirname($recordPath))) mkdir(dirname($recordPath), 0755, true);
    file_put_contents($recordPath, json_encode($record, JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR) . "\n");
}

if (realpath($_SERVER['SCRIPT_FILENAME']) !== __FILE__) return;
$mode = $argv[1] ?? '';
if ($mode === 'capture') {
    file_put_contents($argv[2], json_encode(inventory(), JSON_THROW_ON_ERROR));
} elseif ($mode === 'seed') {
    $baseline = json_decode(file_get_contents($argv[2]), true, 512, JSON_THROW_ON_ERROR);
    echo json_encode(['debian' => release(), 'architecture' => commandOutput(['dpkg', '--print-architecture']),
        'packages' => array_diff_assoc(inventory(), $baseline)], JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR), "\n";
} elseif ($mode === 'install') {
    $lock = readLock($argv[2]);
    $requested = array_slice($argv, 4);
    if (in_array('--all', $requested, true)) $requested = array_values(array_unique([...array_keys($lock['packages']), ...array_diff($requested, ['--all'])]));
    installLocked($argv[2], $argv[3], $requested);
} elseif ($mode === 'runtime') {
    $lock = readLock($argv[2]);
    $record = json_decode(file_get_contents($argv[3]), true, 512, JSON_THROW_ON_ERROR);
    verifyInventory(inventory(), $record['baseline'], $lock['packages'], $record['required']);
} else {
    throw new RuntimeException('Invalid APT lock operation');
}
