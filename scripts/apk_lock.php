<?php
// APK's installed database is authoritative for both apk-tools v2 and v3.
declare(strict_types=1);
function inventory(string $database = '/lib/apk/db/installed'): array {
    $packages = [];
    foreach (explode("\n\n", trim(file_get_contents($database))) as $entry) {
        if (!preg_match('/^P:(.+)$/m', $entry, $name) || !preg_match('/^V:(.+)$/m', $entry, $version)) {
            throw new RuntimeException('Malformed APK inventory');
        }
        if (!str_starts_with($name[1], '.')) $packages[$name[1]] = $version[1];
    }
    ksort($packages);
    return $packages;
}
function verifyInventory(array $current, array $baseline, array $pins): void {
    foreach ($current as $name => $version) {
        if (($pins[$name] ?? $baseline[$name] ?? null) !== $version) {
            throw new RuntimeException("Untracked APK package or version: $name=$version");
        }
    }
    foreach ($baseline as $name => $version) {
        if (!isset($current[$name])) throw new RuntimeException("Base package removed: $name");
    }
    foreach ($pins as $name => $version) {
        if (($current[$name] ?? null) !== $version) throw new RuntimeException("Missing APK pin: $name=$version");
    }
}
function checkBaseVersions(array $baseline, array $pins): void {
    foreach ($pins as $name => $version) {
        if (isset($baseline[$name]) && trim(shell_exec('apk version -t ' . escapeshellarg($baseline[$name]) . ' ' . escapeshellarg($version))) === '>') {
            throw new RuntimeException("APK lock would downgrade base package: $name");
        }
    }
}
if (realpath($_SERVER['SCRIPT_FILENAME']) !== __FILE__) return;
$mode = $argv[1] ?? '';
$baselineFile = $argv[2] ?? '/tmp/php-build/baseline.json';
$current = inventory();
if ($mode === 'capture') {
    file_put_contents($baselineFile, json_encode($current, JSON_THROW_ON_ERROR));
    exit;
}
$baseline = json_decode(file_get_contents($baselineFile), true, 512, JSON_THROW_ON_ERROR);
if ($mode === 'seed') {
    $packages = array_filter($current, fn($v, $k) => ($baseline[$k] ?? null) !== $v, ARRAY_FILTER_USE_BOTH);
    echo json_encode(['alpine' => implode('.', array_slice(explode('.', trim(file_get_contents('/etc/alpine-release'))), 0, 2)),
        'architecture' => trim(shell_exec('apk --print-arch')), 'packages' => $packages], JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR), "\n";
    exit;
}
$lock = json_decode(file_get_contents($argv[3]), true, 512, JSON_THROW_ON_ERROR);
$release = implode('.', array_slice(explode('.', trim(file_get_contents('/etc/alpine-release'))), 0, 2));
if ($release !== $lock['alpine'] || trim(shell_exec('apk --print-arch')) !== $lock['architecture']) {
    throw new RuntimeException('APK lock does not match the base release/architecture');
}
if ($mode === 'check') { checkBaseVersions($baseline, $lock['packages']); exit; }
if ($mode !== 'runtime') throw new RuntimeException('Invalid APK lock operation');
verifyInventory($current, $baseline, $lock['packages']);
