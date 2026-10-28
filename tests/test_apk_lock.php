<?php
declare(strict_types=1);
require __DIR__ . '/../scripts/apk_lock.php';
function rejects(callable $operation): void {
    try { $operation(); } catch (RuntimeException $exception) { return; }
    throw new RuntimeException('Expected lock validation to reject the inventory');
}
$base = ['busybox' => '1.37.0-r31', 'musl' => '1.2.6-r2'];
$pins = ['libzip' => '1.11.4-r0'];
verifyInventory($base + $pins, $base, $pins);
rejects(fn() => verifyInventory($base, $base, $pins));
rejects(fn() => verifyInventory($base + ['libzip' => '1.11.4-r1'], $base, $pins));
rejects(fn() => verifyInventory($base + $pins + ['untracked-lib' => '1.0-r0'], $base, $pins));
rejects(fn() => verifyInventory($base + $pins + ['gcc' => '15.2.0-r5'], $base, $pins));
rejects(fn() => verifyInventory(['musl' => '1.2.6-r2'] + $pins, $base, $pins));
rejects(fn() => verifyInventory(['musl' => '1.2.6-r3', 'busybox' => '1.37.0-r31'] + $pins, $base, $pins));
verifyInventory(['musl' => '1.2.6-r3', 'busybox' => '1.37.0-r31'] + $pins, $base, $pins + ['musl' => '1.2.6-r3']);
checkBaseVersions(['musl' => '1.2.6-r2'], ['musl' => '1.2.6-r3']);
rejects(fn() => checkBaseVersions(['musl' => '1.2.6-r3'], ['musl' => '1.2.6-r2']));
$file = tempnam(sys_get_temp_dir(), 'apk-test-');
file_put_contents($file, "P:busybox\nV:1.37.0-r31\n\nP:.temporary-build\nV:20261024.000000\n\nP:libzip\nV:1.11.4-r0\n");
if (inventory($file) !== ['busybox' => '1.37.0-r31', 'libzip' => '1.11.4-r0']) throw new RuntimeException('Inventory parser failed');
unlink($file);

// Lock metadata is checked by the actual build-time CLI, not the Bake adapter.
$baseline = tempnam(sys_get_temp_dir(), 'apk-baseline-');
$lock = tempnam(sys_get_temp_dir(), 'apk-lock-');
try {
    file_put_contents($baseline, json_encode(inventory(), JSON_THROW_ON_ERROR));
    $release = implode('.', array_slice(explode('.', trim(file_get_contents('/etc/alpine-release'))), 0, 2));
    $architecture = trim(shell_exec('apk --print-arch'));
    $command = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(__DIR__ . '/../scripts/apk_lock.php')
        . ' check ' . escapeshellarg($baseline) . ' ' . escapeshellarg($lock) . ' 2>&1';
    foreach ([[$release, $architecture, 0], ['0.0', $architecture, 1], [$release, 'wrong-architecture', 1]] as [$alpine, $arch, $fails]) {
        file_put_contents($lock, json_encode(['alpine' => $alpine, 'architecture' => $arch, 'packages' => []], JSON_THROW_ON_ERROR));
        exec($command, $output, $status);
        if (($status !== 0) !== (bool) $fails) {
            throw new RuntimeException('Unexpected lock metadata validation result');
        }
    }
} finally {
    unlink($baseline);
    unlink($lock);
}
echo "APK inventory and lock enforcement passed\n";
