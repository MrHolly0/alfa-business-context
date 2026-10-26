<?php
namespace App\Controller;
use App\Database;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\Routing\Attribute\Route;
final class CompanyController {
    private function current(): ?array { return AuthController::user(); }
    #[Route('/api/companies',methods:['GET'])]
    public function list(): JsonResponse {
        if (!$user=$this->current()) return new JsonResponse(['error'=>'Unauthenticated'],401);
        $q=Database::connect()->prepare('SELECT c.id,c.synthetic_u,c.mainokved_name,c.region,m.role FROM companies c JOIN company_memberships m ON m.company_id=c.id WHERE m.user_id=? ORDER BY c.synthetic_u');
        $q->execute([$user['id']]);return new JsonResponse(['companies'=>$q->fetchAll()]);
    }
    #[Route('/api/companies/{id}',methods:['GET'])]
    public function detail(string $id): JsonResponse {
        if (!$user=$this->current()) return new JsonResponse(['error'=>'Unauthenticated'],401);
        $q=Database::connect()->prepare('SELECT c.*,m.role,b.dataset_version FROM companies c JOIN company_memberships m ON m.company_id=c.id JOIN import_batches b ON b.id=c.import_batch_id WHERE c.id=? AND m.user_id=?');
        $q->execute([$id,$user['id']]);$row=$q->fetch();
        if (!$row) return new JsonResponse(['error'=>'Not found'],404);
        unset($row['raw_json'],$row['raw_hash'],$row['source_row']);
        return new JsonResponse(['company'=>$row]);
    }
    #[Route('/api/me/onboarding',methods:['GET'])]
    public function onboarding(): JsonResponse {
        if (!$user=$this->current()) return new JsonResponse(['error'=>'Unauthenticated'],401);
        $q=Database::connect()->prepare('SELECT tour_version,next_step,completed_at FROM onboarding_progress WHERE user_id=?');$q->execute([$user['id']]);
        return new JsonResponse(['progress'=>$q->fetch() ?: ['tour_version'=>1,'next_step'=>0,'completed_at'=>null]]);
    }
    #[Route('/api/me/onboarding',methods:['PUT'])]
    public function saveOnboarding(Request $request): JsonResponse {
        if (!$user=$this->current()) return new JsonResponse(['error'=>'Unauthenticated'],401);
        if (!AuthController::sameOrigin($request)) return new JsonResponse(['error'=>'Bad origin'],403);
        $v=json_decode($request->getContent(),true);if(!is_array($v) || !isset($v['nextStep']) || !is_int($v['nextStep']) || $v['nextStep']<0 || $v['nextStep']>9) return new JsonResponse(['error'=>'Invalid step'],400);
        $db=Database::connect();
        $q=$db->prepare('SELECT next_step,completed_at FROM onboarding_progress WHERE user_id=?');$q->execute([$user['id']]);$current=$q->fetch();
        if ($current && $current['completed_at']) return new JsonResponse(['ok'=>true,'completed'=>true]);
        $next=$current ? (int)$current['next_step'] : 0;
        if ($v['nextStep']<$next || $v['nextStep']>$next+1) return new JsonResponse(['error'=>'Complete steps in order','nextStep'=>$next],409);
        $done=$v['nextStep']===9?gmdate('c'):null;
        $sql='INSERT INTO onboarding_progress(user_id,tour_version,next_step,completed_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET tour_version=excluded.tour_version,next_step=excluded.next_step,completed_at=excluded.completed_at,updated_at=excluded.updated_at';
        $db->prepare($sql)->execute([$user['id'],1,$v['nextStep'],$done,gmdate('c')]);
        return new JsonResponse(['ok'=>true,'completed'=>$done!==null]);
    }
}
