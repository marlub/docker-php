<?php
declare(strict_types=1);
require __DIR__ . '/../scripts/apt_lock.php';

function rejects(callable $operation): void {
    try { $operation(); } catch (RuntimeException $exception) { return; }
    throw new RuntimeException('Expected APT lock validation to reject the inventory');
}

$base = ['libc6' => '2.41-12', 'bash' => '5.2.37-2'];
$pins = ['libzip5' => '1.11.3-1', 'libatomic1' => '14.2.0-19'];
verifyInventory($base + $pins, $base, $pins, array_keys($pins));
verifyInventory($base + ['libatomic1' => $pins['libatomic1']], $base, $pins, ['libatomic1']);
rejects(fn() => verifyInventory($base, $base, $pins, ['libzip5']));
rejects(fn() => verifyInventory($base + ['libzip5' => '1.11.3-2'], $base, $pins, ['libzip5']));
rejects(fn() => verifyInventory($base + $pins + ['gcc' => '14.2.0-19'], $base, $pins, array_keys($pins)));
rejects(fn() => verifyInventory(['libc6' => '2.41-12'] + $pins, $base, $pins, array_keys($pins)));
rejects(fn() => verifyInventory(['libc6' => '2.41-13', 'bash' => '5.2.37-2'], $base, $pins, []));
verifyInventory(['libc6' => '2.41-13', 'bash' => '5.2.37-2'], $base, ['libc6' => '2.41-13'], ['libc6']);
checkBaseVersions(['libc6' => '2.41-12'], ['libc6' => '2.41-12+deb13u1']);
checkBaseVersions(['example' => '1:2.0-1'], ['example' => '2:1.0-1']);
rejects(fn() => checkBaseVersions(['example' => '2:1.0-1'], ['example' => '1:9.0-9']));
rejects(fn() => checkBaseVersions(['example' => '1.0-1'], ['example' => '1.0~rc1-1']));
if (inventory("bash\t5.2.37-2\tinstalled\nremoved\t1.0\tconfig-files\n") !== ['bash' => '5.2.37-2']) {
    throw new RuntimeException('DPKG inventory parser failed');
}
rejects(fn() => inventory('malformed'));

$file = tempnam(sys_get_temp_dir(), 'apt-lock-test-');
try {
    $arch = commandOutput(['dpkg', '--print-architecture']);
    $valid = ['debian' => release(), 'architecture' => $arch, 'packages' => $pins];
    file_put_contents($file, json_encode($valid, JSON_THROW_ON_ERROR));
    readLock($file);
    foreach ([['debian', 'wrong'], ['architecture', 'wrong'], ['packages', ['bad;name' => '1.0']],
              ['packages', ['valid-name' => '1.0;command']]] as [$key, $value]) {
        file_put_contents($file, json_encode(array_replace($valid, [$key => $value]), JSON_THROW_ON_ERROR));
        rejects(fn() => readLock($file));
    }
} finally {
    unlink($file);
}

$record = json_decode(file_get_contents('/usr/local/share/php-build/apt.json'), true, 512, JSON_THROW_ON_ERROR);
$lock = readLock(__DIR__ . '/../build/apt/' . release() . '/' . commandOutput(['dpkg', '--print-architecture']) . '.json');
checkBaseVersions($record['baseline'], $lock['packages']);
verifyInventory(inventory(), $record['baseline'], $lock['packages'], $record['required']);
echo "APT inventory and lock enforcement passed\n";
