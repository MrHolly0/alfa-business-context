<?php
namespace App\Controller;
use App\Database;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\Routing\Attribute\Route;
final class NewsController {
    private function access(string $companyId): ?JsonResponse {
        $user=AuthController::user();if(!$user)return new JsonResponse(['error'=>'Unauthenticated'],401);
        $q=Database::connect()->prepare('SELECT 1 FROM company_memberships WHERE user_id=? AND company_id=?');$q->execute([$user['id'],$companyId]);
        return $q->fetchColumn()?null:new JsonResponse(['error'=>'Not found'],404);
    }
    #[Route('/api/companies/{id}/news/sources',methods:['GET'])]
    public function sources(string $id): JsonResponse {
        if($error=$this->access($id))return $error;
        $q=Database::connect()->prepare('SELECT id,uri,content_type,source_role,classification,title,publisher,published_at,enabled FROM news_sources WHERE company_id=? ORDER BY created_at,id');$q->execute([$id]);
        return new JsonResponse(['sources'=>$q->fetchAll()]);
    }
    #[Route('/api/companies/{id}/news/documents',methods:['GET'])]
    public function documents(string $id,Request $request): JsonResponse {
        if($error=$this->access($id))return $error;
        $from=$request->query->get('from');$to=$request->query->get('to');
        foreach([$from,$to] as $date)if($date!==null && (!is_string($date) || !preg_match('/^\d{4}-\d{2}-\d{2}$/',$date) || !($d=\DateTimeImmutable::createFromFormat('!Y-m-d',$date)) || $d->format('Y-m-d')!==$date))return new JsonResponse(['error'=>'Invalid period'],400);
        if($from && $to && $from>$to)return new JsonResponse(['error'=>'Invalid period'],400);
        $sql='SELECT d.id,d.source_id,d.canonical_uri,d.title,d.publisher,d.author,d.published_at,d.fetched_at,d.latest_revision_ref,d.excerpt,d.classification FROM news_documents d WHERE d.company_id=?';$params=[$id];
        if($from){$sql.=' AND d.published_at>=?';$params[]=$from;}if($to){$sql.=' AND d.published_at<?';$params[]=(new \DateTimeImmutable($to))->modify('+1 day')->format('Y-m-d');}
        $sql.=' ORDER BY d.published_at DESC,d.id LIMIT 100';$q=Database::connect()->prepare($sql);$q->execute($params);
        return new JsonResponse(['documents'=>$q->fetchAll()]);
    }
    #[Route('/api/companies/{id}/news/events/{eventId}/assessment',methods:['GET'])]
    public function assessment(string $id,string $eventId): JsonResponse {
        if($error=$this->access($id))return $error;
        $q=Database::connect()->prepare('SELECT a.id,a.status,a.execution_mode,a.summary,a.candidate_json,a.created_at FROM news_assessments a JOIN business_events e ON e.id=a.event_id WHERE e.company_id=? AND e.id=? ORDER BY a.created_at DESC,a.id DESC LIMIT 1');$q->execute([$id,$eventId]);$row=$q->fetch();
        if(!$row)return new JsonResponse(['error'=>'Not found'],404);
        $row['candidate']=json_decode($row['candidate_json'],true);unset($row['candidate_json']);
        return new JsonResponse(['assessment'=>$row]);
    }
    #[Route('/api/companies/{id}/news/runs',methods:['GET'])]
    public function runs(string $id): JsonResponse {
        if($error=$this->access($id))return $error;
        $q=Database::connect()->prepare('SELECT id,context_version,policy_version,status,error_code,created_at,finished_at FROM news_runs WHERE company_id=? ORDER BY created_at DESC LIMIT 50');$q->execute([$id]);
        return new JsonResponse(['runs'=>$q->fetchAll()]);
    }
    #[Route('/api/companies/{id}/news/runs/{runId}',methods:['GET'])]
    public function run(string $id,string $runId): JsonResponse {
        if($error=$this->access($id))return $error;
        $q=Database::connect()->prepare('SELECT id,context_version,policy_version,status,error_code,result_json,created_at,finished_at FROM news_runs WHERE company_id=? AND id=?');$q->execute([$id,$runId]);$row=$q->fetch();
        if(!$row)return new JsonResponse(['error'=>'Not found'],404);
        $row['result']=$row['result_json']?json_decode($row['result_json'],true):null;unset($row['result_json']);
        return new JsonResponse(['run'=>$row]);
    }
}
