<?php
namespace App\Controller;

use App\Database;
use PDO;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\Routing\Attribute\Route;

final class BusinessController {
    private function access(string $companyId, bool $write=false): array|JsonResponse {
        $user=AuthController::user();
        if (!$user) return new JsonResponse(['error'=>'Unauthenticated'],401);
        $q=Database::connect()->prepare('SELECT role FROM company_memberships WHERE user_id=? AND company_id=?');
        $q->execute([$user['id'],$companyId]);
        $role=$q->fetchColumn();
        if (!$role) return new JsonResponse(['error'=>'Not found'],404);
        if ($write && $role==='viewer') return new JsonResponse(['error'=>'Forbidden'],403);
        return ['user'=>$user,'role'=>$role];
    }
    private function input(Request $request): array|JsonResponse {
        if (!AuthController::sameOrigin($request)) return new JsonResponse(['error'=>'Bad origin'],403);
        if (!str_starts_with((string)$request->headers->get('Content-Type'),'application/json')) return new JsonResponse(['error'=>'JSON required'],415);
        $object=json_decode($request->getContent());
        return $object instanceof \stdClass ? json_decode($request->getContent(),true) : new JsonResponse(['error'=>'Invalid JSON object'],400);
    }
    private function string(mixed $value, int $max, bool $required=true): ?string {
        if (!is_string($value)) return null;
        $value=trim($value);
        if (($required && $value==='') || mb_strlen($value)>$max) return null;
        return $value;
    }
    private function date(mixed $value): ?string {
        if (!is_string($value) || !preg_match('/^\d{4}-\d{2}-\d{2}$/',$value)) return null;
        $d=\DateTimeImmutable::createFromFormat('!Y-m-d',$value);
        return $d && $d->format('Y-m-d')===$value ? $value : null;
    }
    private function metric(PDO $db,string $companyId,string $metricId): ?array {
        $q=$db->prepare('SELECT * FROM metric_definitions WHERE id=? AND company_id=? AND archived_at IS NULL');
        $q->execute([$metricId,$companyId]);return $q->fetch() ?: null;
    }
    private function latestMetrics(PDO $db,string $companyId,?string $from=null,?string $to=null): array {
        $q=$db->prepare('SELECT d.id,d.slug,d.created_at,v.version,v.title,v.description,v.unit,v.direction FROM metric_definitions d JOIN metric_versions v ON v.metric_id=d.id AND v.version=(SELECT MAX(v2.version) FROM metric_versions v2 WHERE v2.metric_id=d.id) WHERE d.company_id=? AND d.archived_at IS NULL ORDER BY d.created_at,d.id');
        $q->execute([$companyId]);$metrics=$q->fetchAll();
        $sql='SELECT id,value_raw,unit,period_start,period_end,source_label,created_at FROM metric_observations WHERE metric_id=?';
        if ($from) $sql.=' AND period_end>=?';
        if ($to) $sql.=' AND period_start<=?';
        $sql.=' ORDER BY period_end DESC,created_at DESC LIMIT 1';
        $obs=$db->prepare($sql);
        foreach($metrics as &$metric) {
            $params=[$metric['id']];if($from)$params[]=$from;if($to)$params[]=$to;
            $obs->execute($params);$metric['observation']=$obs->fetch() ?: null;
            $metric['status']=$metric['observation'] ? 'observed' : 'no_data';
        }
        return $metrics;
    }
    #[Route('/api/companies/{id}/context',methods:['GET'])]
    public function context(string $id): JsonResponse {
        if (($a=$this->access($id)) instanceof JsonResponse) return $a;
        $q=Database::connect()->prepare('SELECT version,facts_json,source,created_at FROM company_context_versions WHERE company_id=? ORDER BY version DESC LIMIT 1');
        $q->execute([$id]);$row=$q->fetch();
        return new JsonResponse(['context'=>$row ? ['version'=>(int)$row['version'],'facts'=>json_decode($row['facts_json'],true),'source'=>$row['source'],'createdAt'=>$row['created_at']] : ['version'=>0,'facts'=>new \stdClass()]]);
    }
    #[Route('/api/companies/{id}/context',methods:['PUT'])]
    public function saveContext(string $id,Request $request): JsonResponse {
        if (($a=$this->access($id,true)) instanceof JsonResponse) return $a;
        if (($v=$this->input($request)) instanceof JsonResponse) return $v;
        if (!isset($v['expectedVersion']) || !is_int($v['expectedVersion']) || $v['expectedVersion']<0 || !isset($v['facts']) || !is_array($v['facts']) || ($v['facts']!==[] && array_is_list($v['facts']))) return new JsonResponse(['error'=>'Expected version and facts object required'],400);
        $allowed=['activities','products','markets','suppliers','customers','risks','notes'];
        foreach($v['facts'] as $key=>$items) {
            if (!in_array($key,$allowed,true) || !is_array($items) || count($items)>30) return new JsonResponse(['error'=>'Invalid facts'],400);
            foreach($items as $item) if (!$this->string($item,300)) return new JsonResponse(['error'=>'Invalid fact'],400);
        }
        $json=json_encode($v['facts'],JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR);
        if (strlen($json)>30000) return new JsonResponse(['error'=>'Context too large'],400);
        $db=Database::connect();$db->beginTransaction();
        try {
            Database::lockCompany($db,$id);
            $q=$db->prepare('SELECT MAX(version) FROM company_context_versions WHERE company_id=?');$q->execute([$id]);$current=(int)$q->fetchColumn();
            if ($current!==$v['expectedVersion']) {$db->rollBack();return new JsonResponse(['error'=>'Version conflict','currentVersion'=>$current],409);}
            $new=$current+1;
            $db->prepare('INSERT INTO company_context_versions(id,company_id,version,facts_json,source,confirmed_by,created_at) VALUES(?,?,?,?,?,?,?)')->execute([bin2hex(random_bytes(16)),$id,$new,$json,'user',$a['user']['id'],gmdate('c')]);
            $db->commit();return new JsonResponse(['version'=>$new],201);
        } catch (\Throwable $e) {$db->rollBack();throw $e;}
    }
    #[Route('/api/companies/{id}/metrics',methods:['GET'])]
    public function metrics(string $id): JsonResponse {
        if (($a=$this->access($id)) instanceof JsonResponse) return $a;
        return new JsonResponse(['metrics'=>$this->latestMetrics(Database::connect(),$id)]);
    }
    #[Route('/api/companies/{id}/metrics',methods:['POST'])]
    public function createMetric(string $id,Request $request): JsonResponse {
        if (($a=$this->access($id,true)) instanceof JsonResponse) return $a;
        if (($v=$this->input($request)) instanceof JsonResponse) return $v;
        $title=$this->string($v['title'] ?? null,160);$description=$this->string($v['description'] ?? null,2000);
        $unit=$this->string($v['unit'] ?? '',40,false);$direction=$v['direction'] ?? 'neutral';
        if (!$title || !$description || $unit===null || !in_array($direction,['up','down','neutral'],true)) return new JsonResponse(['error'=>'Invalid metric'],400);
        $db=Database::connect();$db->beginTransaction();
        try {
            $metricId=bin2hex(random_bytes(16));$now=gmdate('c');
            $db->prepare('INSERT INTO metric_definitions(id,company_id,slug,created_by,created_at) VALUES(?,?,?,?,?)')->execute([$metricId,$id,$metricId,$a['user']['id'],$now]);
            $db->prepare('INSERT INTO metric_versions(id,metric_id,version,title,description,unit,direction,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)')->execute([bin2hex(random_bytes(16)),$metricId,1,$title,$description,$unit,$direction,$a['user']['id'],$now]);
            $db->commit();return new JsonResponse(['id'=>$metricId,'version'=>1,'status'=>'no_data'],201);
        } catch (\Throwable $e) {$db->rollBack();throw $e;}
    }
    #[Route('/api/companies/{id}/metrics/{metricId}',methods:['PATCH'])]
    public function updateMetric(string $id,string $metricId,Request $request): JsonResponse {
        if (($a=$this->access($id,true)) instanceof JsonResponse) return $a;
        if (($v=$this->input($request)) instanceof JsonResponse) return $v;
        $db=Database::connect();if(!$this->metric($db,$id,$metricId))return new JsonResponse(['error'=>'Not found'],404);
        if (!isset($v['expectedVersion']) || !is_int($v['expectedVersion'])) return new JsonResponse(['error'=>'Expected version required'],400);
        $q=$db->prepare('SELECT * FROM metric_versions WHERE metric_id=? ORDER BY version DESC LIMIT 1');$q->execute([$metricId]);$old=$q->fetch();
        if ((int)$old['version']!==$v['expectedVersion']) return new JsonResponse(['error'=>'Version conflict','currentVersion'=>(int)$old['version']],409);
        $title=$this->string($v['title'] ?? $old['title'],160);$description=$this->string($v['description'] ?? $old['description'],2000);
        $unit=$this->string($v['unit'] ?? $old['unit'],40,false);$direction=$v['direction'] ?? $old['direction'];
        if (!$title || !$description || $unit===null || !in_array($direction,['up','down','neutral'],true)) return new JsonResponse(['error'=>'Invalid metric'],400);
        $version=(int)$old['version']+1;
        $db->prepare('INSERT INTO metric_versions(id,metric_id,version,title,description,unit,direction,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)')->execute([bin2hex(random_bytes(16)),$metricId,$version,$title,$description,$unit,$direction,$a['user']['id'],gmdate('c')]);
        return new JsonResponse(['id'=>$metricId,'version'=>$version]);
    }
    #[Route('/api/companies/{id}/metrics/{metricId}/observations',methods:['POST'])]
    public function addObservation(string $id,string $metricId,Request $request): JsonResponse {
        if (($a=$this->access($id,true)) instanceof JsonResponse) return $a;
        if (($v=$this->input($request)) instanceof JsonResponse) return $v;
        $db=Database::connect();if(!$this->metric($db,$id,$metricId))return new JsonResponse(['error'=>'Not found'],404);
        $q=$db->prepare('SELECT unit FROM metric_versions WHERE metric_id=? ORDER BY version DESC LIMIT 1');$q->execute([$metricId]);$metricUnit=$q->fetchColumn();
        $value=$v['value'] ?? null;$unit=$this->string($v['unit'] ?? '',40,false);$source=$this->string($v['source'] ?? null,200);
        $from=$this->date($v['periodStart'] ?? null);$to=$this->date($v['periodEnd'] ?? null);
        if ((!is_string($value) && !is_int($value) && !is_float($value)) || !preg_match('/^-?\d+(?:\.\d+)?$/',(string)$value) || strlen((string)$value)>100 || ($unit===null || ($metricUnit!=='' && $unit!==$metricUnit)) || !$source || !$from || !$to || $from>$to) return new JsonResponse(['error'=>'Invalid observation'],400);
        $idObs=bin2hex(random_bytes(16));
        $db->prepare('INSERT INTO metric_observations(id,metric_id,value_raw,unit,period_start,period_end,source_label,entered_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)')->execute([$idObs,$metricId,(string)$value,$unit,$from,$to,$source,$a['user']['id'],gmdate('c')]);
        return new JsonResponse(['id'=>$idObs],201);
    }
    #[Route('/api/companies/{id}/dashboard',methods:['GET'])]
    public function dashboard(string $id,Request $request): JsonResponse {
        if (($a=$this->access($id)) instanceof JsonResponse) return $a;
        $from=$request->query->get('from');$to=$request->query->get('to');
        if (($from && !$this->date($from)) || ($to && !$this->date($to)) || ($from && $to && $from>$to)) return new JsonResponse(['error'=>'Invalid period'],400);
        $db=Database::connect();$metrics=$this->latestMetrics($db,$id,$from,$to);
        $sql='SELECT e.id,e.title,e.summary,e.kind,e.published_on,e.source_label,e.source_url,(SELECT a.status FROM news_assessments a WHERE a.event_id=e.id ORDER BY a.created_at DESC,a.id DESC LIMIT 1) AS assessment_status,(SELECT a.summary FROM news_assessments a WHERE a.event_id=e.id ORDER BY a.created_at DESC,a.id DESC LIMIT 1) AS assessment_summary FROM business_events e WHERE e.company_id=?';$params=[$id];
        if($from){$sql.=' AND e.published_on>=?';$params[]=$from;}if($to){$sql.=' AND e.published_on<=?';$params[]=$to;}
        $sql.=' ORDER BY e.published_on DESC,e.id LIMIT 50';$q=$db->prepare($sql);$q->execute($params);
        return new JsonResponse(['period'=>['from'=>$from,'to'=>$to],'summary'=>['status'=>'no_analysis','text'=>null],'metrics'=>$metrics,'events'=>$q->fetchAll()]);
    }
    #[Route('/api/companies/{id}/events',methods:['POST'])]
    public function addEvent(string $id,Request $request): JsonResponse {
        if (($a=$this->access($id,true)) instanceof JsonResponse) return $a;
        if (($v=$this->input($request)) instanceof JsonResponse) return $v;
        $title=$this->string($v['title'] ?? null,240);$summary=$this->string($v['summary'] ?? null,4000);
        $kind=$this->string($v['kind'] ?? null,40);$date=$this->date($v['publishedOn'] ?? null);
        $source=$this->string($v['source'] ?? null,200);$url=$this->string($v['sourceUrl'] ?? '',2000,false);
        if (!$title || !$summary || !$kind || !$date || !$source || $url===null || ($url!=='' && (!filter_var($url,FILTER_VALIDATE_URL) || !in_array(parse_url($url,PHP_URL_SCHEME),['https','http'],true)))) return new JsonResponse(['error'=>'Invalid event'],400);
        $eventId=bin2hex(random_bytes(16));
        Database::connect()->prepare('INSERT INTO business_events(id,company_id,title,summary,kind,published_on,source_label,source_url,created_at) VALUES(?,?,?,?,?,?,?,?,?)')->execute([$eventId,$id,$title,$summary,$kind,$date,$source,$url,gmdate('c')]);
        return new JsonResponse(['id'=>$eventId],201);
    }
    #[Route('/api/companies/{id}/metrics/{metricId}/events',methods:['GET'])]
    public function metricEvents(string $id,string $metricId,Request $request): JsonResponse {
        if (($a=$this->access($id)) instanceof JsonResponse) return $a;
        $db=Database::connect();if(!$this->metric($db,$id,$metricId))return new JsonResponse(['error'=>'Not found'],404);
        $from=$request->query->get('from');$to=$request->query->get('to');
        if (($from && !$this->date($from)) || ($to && !$this->date($to)) || ($from && $to && $from>$to))return new JsonResponse(['error'=>'Invalid period'],400);
        $sql='SELECT e.id,e.title,e.summary,e.kind,e.published_on,e.source_label,e.source_url,l.explanation,l.direction,l.status FROM metric_event_links l JOIN business_events e ON e.id=l.event_id WHERE l.metric_id=? AND e.company_id=?';$params=[$metricId,$id];
        if($from){$sql.=' AND e.published_on>=?';$params[]=$from;}if($to){$sql.=' AND e.published_on<=?';$params[]=$to;}
        $sql.=' ORDER BY e.published_on DESC,e.id';$q=$db->prepare($sql);$q->execute($params);
        return new JsonResponse(['events'=>$q->fetchAll()]);
    }
    #[Route('/api/companies/{id}/metrics/{metricId}/events/{eventId}',methods:['PUT'])]
    public function linkEvent(string $id,string $metricId,string $eventId,Request $request): JsonResponse {
        if (($a=$this->access($id,true)) instanceof JsonResponse) return $a;
        if (($v=$this->input($request)) instanceof JsonResponse) return $v;
        $db=Database::connect();if(!$this->metric($db,$id,$metricId))return new JsonResponse(['error'=>'Not found'],404);
        $q=$db->prepare('SELECT id FROM business_events WHERE id=? AND company_id=?');$q->execute([$eventId,$id]);if(!$q->fetchColumn())return new JsonResponse(['error'=>'Not found'],404);
        $explanation=$this->string($v['explanation'] ?? null,2000);$direction=$v['direction'] ?? null;
        if (!$explanation || !in_array($direction,['up','down','mixed','unknown'],true))return new JsonResponse(['error'=>'Invalid link'],400);
        $db->prepare('INSERT INTO metric_event_links(metric_id,event_id,explanation,direction,status,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(metric_id,event_id) DO UPDATE SET explanation=excluded.explanation,direction=excluded.direction,status=excluded.status')->execute([$metricId,$eventId,$explanation,$direction,'confirmed',gmdate('c')]);
        return new JsonResponse(['ok'=>true]);
    }
    private function validSubject(PDO $db,string $companyId,string $type,mixed $subject): bool {
        if ($type==='dashboard')return $subject===null;
        if (!is_string($subject))return false;
        if ($type==='metric')return $this->metric($db,$companyId,$subject)!==null;
        $q=$db->prepare('SELECT id FROM business_events WHERE id=? AND company_id=?');$q->execute([$subject,$companyId]);return (bool)$q->fetchColumn();
    }
    #[Route('/api/companies/{id}/feedback',methods:['POST'])]
    public function feedback(string $id,Request $request): JsonResponse {
        if (($a=$this->access($id)) instanceof JsonResponse)return $a;
        if (($v=$this->input($request)) instanceof JsonResponse)return $v;
        $type=$v['subjectType'] ?? null;$kind=$v['kind'] ?? null;$comment=$this->string($v['comment'] ?? '',2000,false);
        $db=Database::connect();if(!in_array($type,['metric','event','dashboard'],true) || !in_array($kind,['relevant','irrelevant','incorrect','missing_context','other'],true) || $comment===null || !$this->validSubject($db,$id,$type,$v['subjectId'] ?? null))return new JsonResponse(['error'=>'Invalid feedback'],400);
        $fid=bin2hex(random_bytes(16));$db->prepare('INSERT INTO feedback(id,company_id,user_id,subject_type,subject_id,kind,comment,created_at) VALUES(?,?,?,?,?,?,?,?)')->execute([$fid,$id,$a['user']['id'],$type,$v['subjectId'] ?? null,$kind,$comment,gmdate('c')]);
        return new JsonResponse(['id'=>$fid],201);
    }
    #[Route('/api/companies/{id}/discussions',methods:['GET'])]
    public function discussions(string $id,Request $request): JsonResponse {
        if (($a=$this->access($id)) instanceof JsonResponse)return $a;
        $type=$request->query->get('subjectType');$subject=$request->query->get('subjectId');$db=Database::connect();
        if(!in_array($type,['metric','event','dashboard'],true) || !$this->validSubject($db,$id,$type,$subject))return new JsonResponse(['error'=>'Invalid subject'],400);
        $q=$db->prepare('SELECT d.id,d.parent_id,d.body,d.created_at,u.display_name FROM discussions d JOIN app_users u ON u.id=d.user_id WHERE d.company_id=? AND d.subject_type=? AND ((d.subject_id IS NULL AND ? IS NULL) OR d.subject_id=?) ORDER BY d.created_at,d.id LIMIT 200');
        $q->execute([$id,$type,$subject,$subject]);return new JsonResponse(['messages'=>$q->fetchAll()]);
    }
    #[Route('/api/companies/{id}/discussions',methods:['POST'])]
    public function addDiscussion(string $id,Request $request): JsonResponse {
        if (($a=$this->access($id)) instanceof JsonResponse)return $a;
        if (($v=$this->input($request)) instanceof JsonResponse)return $v;
        $type=$v['subjectType'] ?? null;$subject=$v['subjectId'] ?? null;$body=$this->string($v['body'] ?? null,4000);$parent=$v['parentId'] ?? null;$db=Database::connect();
        if(!in_array($type,['metric','event','dashboard'],true) || !$body || !$this->validSubject($db,$id,$type,$subject))return new JsonResponse(['error'=>'Invalid message'],400);
        if($parent!==null){$q=$db->prepare('SELECT id FROM discussions WHERE id=? AND company_id=? AND subject_type=? AND ((subject_id IS NULL AND ? IS NULL) OR subject_id=?)');$q->execute([$parent,$id,$type,$subject,$subject]);if(!$q->fetchColumn())return new JsonResponse(['error'=>'Invalid parent'],400);}
        $mid=bin2hex(random_bytes(16));$db->prepare('INSERT INTO discussions(id,company_id,user_id,subject_type,subject_id,parent_id,body,created_at) VALUES(?,?,?,?,?,?,?,?)')->execute([$mid,$id,$a['user']['id'],$type,$subject,$parent,$body,gmdate('c')]);
        return new JsonResponse(['id'=>$mid],201);
    }
}
