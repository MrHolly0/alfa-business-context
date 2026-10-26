<?php
namespace App\Intelligence;

final class SourceUriPolicy
{
    public static function allowsDocumentUri(string $uri, array $source): bool
    {
        $parts = parse_url($uri);
        if (!is_array($parts) || ($parts['scheme'] ?? '') !== 'https'
            || empty($parts['host']) || isset($parts['user']) || isset($parts['pass'])) {
            return false;
        }

        $sourceUri = parse_url((string)($source['uri'] ?? ''), PHP_URL_HOST);
        if (!is_string($sourceUri) || $sourceUri === '') {
            return false;
        }
        $allowed = [self::normalizeHost($sourceUri)];
        try {
            $articleHosts = json_decode((string)($source['article_hosts_json'] ?? '[]'), false, 16, JSON_THROW_ON_ERROR);
        } catch (\JsonException) {
            return false;
        }
        if (!is_array($articleHosts) || !array_is_list($articleHosts)) {
            return false;
        }
        foreach ($articleHosts as $articleHost) {
            if (!is_string($articleHost) || !preg_match('/^[a-z0-9.-]+$/i', $articleHost)) {
                return false;
            }
            $allowed[] = self::normalizeHost($articleHost);
        }

        return in_array(self::normalizeHost($parts['host']), $allowed, true);
    }

    private static function normalizeHost(string $host): string
    {
        return strtolower(rtrim($host, '.'));
    }
}
