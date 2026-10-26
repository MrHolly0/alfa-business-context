<?php
namespace App\Controller;

use App\Database;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\Routing\Attribute\Route;

final class HealthController
{
    #[Route('/health', name: 'health', methods: ['GET'])]
    public function health(): JsonResponse
    {
        try {
            $db = Database::connect();
            $db->query('SELECT 1')->fetchColumn();
            $db->query('SELECT 1 FROM app_users LIMIT 0');
        } catch (\Throwable) {
            return new JsonResponse(['service' => 'core', 'status' => 'unavailable'], JsonResponse::HTTP_SERVICE_UNAVAILABLE);
        }

        return new JsonResponse(['service' => 'core', 'status' => 'ok']);
    }
}
