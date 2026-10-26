<?php
namespace App\Controller;
use App\Database;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\Routing\Attribute\Route;
final class NewsController {
    private function access(string $companyId, bool $write=false): ?JsonResponse {
        $user=AuthController::user();if(!$user)return new JsonResponse(['error'=>'Unauthenticated'],401);
        $q=Database::connect()->prepare('SELECT role FROM company_memberships WHERE user_id=? AND company_id=?');$q->execute([$user['id'],$companyId]);$role=$q->fetchColumn();
        if(!$role)return new JsonResponse(['error'=>'Not found'],404);
        if($write && !in_array($role,['admin','editor'],true))return new JsonResponse(['error'=>'Forbidden'],403);
        return null;
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
        $q=Database::connect()->prepare('SELECT id,context_version,policy_version,status,error_code,created_at,finished_at FROM news_runs WHERE company_id=? ORDER BY created_at DESC,id DESC LIMIT 50');$q->execute([$id]);
        return new JsonResponse(['runs'=>$q->fetchAll()]);
    }
    #[Route('/api/companies/{id}/news/runs',methods:['POST'])]
    public function startRun(string $id, Request $request): JsonResponse {
        if($error=$this->access($id,true))return $error;
        if(!AuthController::sameOrigin($request))return new JsonResponse(['error'=>'Bad origin'],403);
        if(!str_starts_with((string)$request->headers->get('Content-Type'),'application/json'))return new JsonResponse(['error'=>'JSON required'],415);
        $input=json_decode($request->getContent());
        if(!$input instanceof \stdClass)return new JsonResponse(['error'=>'Invalid JSON object'],400);
        $input=get_object_vars($input);
        if(array_diff(array_keys($input),['maxEvents']) || (isset($input['maxEvents']) && $input['maxEvents']!==1))return new JsonResponse(['error'=>'maxEvents must be 1'],400);
        // Release PHP's per-session file lock before the synchronous Intelligence call.
        if(session_status()===PHP_SESSION_ACTIVE)session_write_close();
        $runId=bin2hex(random_bytes(16));
        $judgingCase=getenv('CORE_JUDGING_CASE')==='1';
        $internalTimeout=$judgingCase?600:180;$processTimeout=$internalTimeout+10;
        // PHP_BINARY may point at the Apache module host in mod_php. Spawn the
        // installed CLI binary explicitly so the same route works under Apache.
        $phpCli=rtrim(PHP_BINDIR,'/').'/php';
        if(!is_executable($phpCli))$phpCli=PHP_BINARY;
        $command=[$phpCli,dirname(__DIR__,2).'/bin/run-news.php',$id,'--max-events','1','--request-id',$runId,'--timeout',(string)$internalTimeout];
        $db=Database::connect();
        try { [$status,$stdout,$stderr]=$this->executeRunCommand($command,dirname(__DIR__,2),$processTimeout); }
        catch(\Throwable $e) {
            $now=gmdate('c');
            $errorCode=$this->runProcessErrorCode($e, $command[1]);
            error_log('core_news_run process_error='.$errorCode.' sapi='.PHP_SAPI);
            $db->prepare("UPDATE news_runs SET status='failed',error_code=?,finished_at=? WHERE id=? AND company_id=? AND status='running'")->execute(['http_'.$errorCode,$now,$runId,$id]);
            return new JsonResponse(['error'=>'analysis_process_unavailable'],502);
        }
        $query=$db->prepare('SELECT id,context_version,policy_version,status,error_code,created_at,finished_at,result_json FROM news_runs WHERE id=? AND company_id=?');
        $query->execute([$runId,$id]);$run=$query->fetch();
        if(!$run) {
            $active=$db->prepare("SELECT 1 FROM news_runs WHERE company_id=? AND status='running' LIMIT 1");$active->execute([$id]);
            if($active->fetchColumn() || str_contains($stdout,'analysis_in_progress'))return new JsonResponse(['error'=>'analysis_in_progress'],409);
            if(str_contains($stdout,'no_enabled_sources'))return new JsonResponse(['error'=>'no_enabled_sources'],409);
            if(str_contains($stdout,'no_source_policy'))return new JsonResponse(['error'=>'no_source_policy'],409);
            return new JsonResponse(['error'=>'analysis_run_not_created'],502);
        }
        $run['result']=$run['result_json']?json_decode($run['result_json'],true):null;unset($run['result_json']);
        if($run['status']==='running') {
            $db->prepare("UPDATE news_runs SET status='failed',error_code='http_process_timeout',finished_at=? WHERE id=? AND status='running'")->execute([gmdate('c'),$runId]);
            $run['status']='failed';$run['error_code']='http_process_timeout';$run['finished_at']=gmdate('c');
        }
        if($status!==0 || !in_array($run['status'],['completed','partial','no_change','unknown'],true))return new JsonResponse(['error'=>'analysis_failed','run'=>$run],502);
        return new JsonResponse(['run'=>$run]);
    }
    #[Route('/api/companies/{id}/news/runs/{runId}',methods:['GET'])]
    public function run(string $id,string $runId): JsonResponse {
        if($error=$this->access($id))return $error;
        $q=Database::connect()->prepare('SELECT id,context_version,policy_version,status,error_code,result_json,created_at,finished_at FROM news_runs WHERE company_id=? AND id=?');$q->execute([$id,$runId]);$row=$q->fetch();
        if(!$row)return new JsonResponse(['error'=>'Not found'],404);
        $row['result']=$row['result_json']?json_decode($row['result_json'],true):null;unset($row['result_json']);
        return new JsonResponse(['run'=>$row]);
    }

    private function executeRunCommand(array $command,string $cwd,int $timeoutSeconds): array {
        $process=proc_open($command,[0=>['pipe','r'],1=>['pipe','w'],2=>['pipe','w']],$pipes,$cwd);
        if(!is_resource($process))throw new \RuntimeException('analysis_process_start_failed');
        fclose($pipes[0]);stream_set_blocking($pipes[1],false);stream_set_blocking($pipes[2],false);
        $stdout='';$stderr='';$started=microtime(true);$exitCode=null;$timedOut=false;
        do {
            $stdout.=stream_get_contents($pipes[1]);$stderr.=stream_get_contents($pipes[2]);
            if(strlen($stdout)>32_000_000 || strlen($stderr)>1_000_000){proc_terminate($process,9);throw new \RuntimeException('analysis_process_output_limit');}
            $status=proc_get_status($process);
            if(!$status['running']){$exitCode=$status['exitcode'];break;}
            if(microtime(true)-$started>$timeoutSeconds){$timedOut=true;proc_terminate($process,15);usleep(200000);$status=proc_get_status($process);if($status['running'])proc_terminate($process,9);break;}
            usleep(100000);
        } while(true);
        $stdout.=stream_get_contents($pipes[1]);$stderr.=stream_get_contents($pipes[2]);fclose($pipes[1]);fclose($pipes[2]);$closed=proc_close($process);
        if($timedOut)throw new \RuntimeException('analysis_process_timeout');
        if($exitCode===null || $exitCode<0)$exitCode=$closed;
        return [$exitCode,$stdout,$stderr];
    }

    private function runProcessErrorCode(\Throwable $error,string $runnerPath): string {
        if($error->getMessage()==='analysis_process_timeout')return 'process_timeout';
        if($error->getMessage()==='analysis_process_output_limit')return 'process_output_limit';
        if(!function_exists('proc_open') || in_array('proc_open',explode(',',ini_get('disable_functions') ?: ''),true))return 'proc_open_unavailable';
        $phpCli=rtrim(PHP_BINDIR,'/').'/php';
        if(!is_executable($phpCli) && !is_executable(PHP_BINARY))return 'php_binary_unavailable';
        if(!is_file($runnerPath) || !is_readable($runnerPath))return 'runner_unavailable';
        if($error->getMessage()==='analysis_process_start_failed')return 'process_start_failed';
        return 'process_internal_error';
    }
}
