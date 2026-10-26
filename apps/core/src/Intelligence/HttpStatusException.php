<?php
namespace App\Intelligence;

use RuntimeException;

final class HttpStatusException extends RuntimeException {
    public function __construct(public readonly int $status) {
        if($status<300 || $status>599)throw new \InvalidArgumentException('HTTP failure status must be between 300 and 599');
        parent::__construct('intelligence_http_status');
    }

    public function safeErrorCode(): string { return 'intelligence_http_'.$this->status; }
}
