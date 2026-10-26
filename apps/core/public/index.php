<?php
use App\Kernel;
require dirname(__DIR__).'/vendor/autoload_runtime.php';
return function(array $context): Kernel {
    return new Kernel($context['APP_ENV'] ?? 'prod', (bool)($context['APP_DEBUG'] ?? false));
};
