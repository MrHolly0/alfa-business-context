<?php
namespace App;
use PDO;
final class Database {
    public static function connect(): PDO {
        $url = getenv('DATABASE_URL');
        if (!$url) {
            $path = getenv('CORE_SQLITE_PATH') ?: dirname(__DIR__).'/var/core.sqlite';
            $directory = dirname($path);
            if (!is_dir($directory) && !mkdir($directory, 0770, true) && !is_dir($directory)) {
                throw new \RuntimeException('Cannot create SQLite directory');
            }
            $db = new PDO('sqlite:'.$path);
            $db->exec('PRAGMA foreign_keys=ON');
            $db->exec('PRAGMA busy_timeout=5000');
        } else {
            $parts = parse_url($url);
            if (!$parts || !in_array($parts['scheme'] ?? '', ['postgres','postgresql'],true)) throw new \RuntimeException('DATABASE_URL must be PostgreSQL');
            $db = new PDO(sprintf('pgsql:host=%s;port=%d;dbname=%s',$parts['host'], $parts['port'] ?? 5432,ltrim($parts['path'] ?? '','/')),urldecode($parts['user'] ?? ''),urldecode($parts['pass'] ?? ''));
        }
        $db->setAttribute(PDO::ATTR_ERRMODE,PDO::ERRMODE_EXCEPTION);
        $db->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE,PDO::FETCH_ASSOC);
        return $db;
    }
}
