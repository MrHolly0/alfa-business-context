<?php
namespace App;

use PDO;

final class NewsRunFence {
    public static function isActive(PDO $db, string $companyId, string $runId): bool {
        $query=$db->prepare("SELECT 1 FROM news_runs WHERE id=? AND company_id=? AND status='running'");
        $query->execute([$runId,$companyId]);
        return (bool)$query->fetchColumn();
    }
}
