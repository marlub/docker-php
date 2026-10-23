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
echo "APK inventory and lock enforcement passed\n";
