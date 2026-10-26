<?php
require dirname(__DIR__).'/vendor/autoload.php';

function runLocalCli(array $command,string $cwd): array {
    $process=proc_open($command,[0=>['pipe','r'],1=>['pipe','w'],2=>['pipe','w']],$pipes,$cwd);
    if(!is_resource($process))throw new RuntimeException('local_process_start_failed');
    fclose($pipes[0]);stream_set_blocking($pipes[1],false);stream_set_blocking($pipes[2],false);
    $output='';$errors='';$started=microtime(true);$exitCode=null;$timedOut=false;
    do {
        $output.=stream_get_contents($pipes[1]);$errors.=stream_get_contents($pipes[2]);
        if(strlen($output)>1_000_000 || strlen($errors)>1_000_000){proc_terminate($process,9);throw new RuntimeException('local_process_output_limit');}
        $status=proc_get_status($process);
        if(!$status['running']){$exitCode=$status['exitcode'];break;}
        if(microtime(true)-$started>600){$timedOut=true;proc_terminate($process,15);usleep(200000);$status=proc_get_status($process);if($status['running'])proc_terminate($process,9);break;}
        usleep(100000);
    } while(true);
    $output.=stream_get_contents($pipes[1]);$errors.=stream_get_contents($pipes[2]);
    fclose($pipes[1]);fclose($pipes[2]);$closed=proc_close($process);
    if($timedOut)throw new RuntimeException('local_process_timeout');
    if($exitCode===null || $exitCode<0)$exitCode=$closed;
    return [$exitCode,$output,$errors];
}

function postToIntelligence(array $task,string $tenantId): array {
    $base=getenv('INTELLIGENCE_BASE_URL');
    $token=getenv('INTELLIGENCE_SERVICE_TOKEN');
    if($token===false || $token===''){
        $tokenPath=getenv('INTELLIGENCE_SERVICE_TOKEN_FILE') ?: '/run/intelligence-auth/service-token';
        if(!is_string($tokenPath) || !is_file($tokenPath) || !is_readable($tokenPath))throw new RuntimeException('service_token_unavailable');
        $size=filesize($tokenPath);if($size===false || $size<1 || $size>4096)throw new RuntimeException('service_token_invalid');
        $token=trim((string)file_get_contents($tokenPath));
    }
    if(!is_string($token) || $token==='' || strlen($token)>4096 || preg_match('/[\r\n\x00-\x1f\x7f]/',$token))throw new RuntimeException('service_token_invalid');
    if(!is_string($base) || $base==='')throw new RuntimeException('intelligence_base_url_missing');
    $parts=parse_url($base);
    if(!$parts || !in_array($parts['scheme'] ?? '',['http','https'],true) || empty($parts['host']) || isset($parts['user']) || isset($parts['pass']) || isset($parts['query']) || isset($parts['fragment']) || !in_array($parts['path'] ?? '', ['', '/'],true))throw new RuntimeException('intelligence_base_url_invalid');
    $timeoutValue=getenv('INTELLIGENCE_TIMEOUT_SECONDS');
    $timeout=$timeoutValue===false || $timeoutValue===''?180:filter_var($timeoutValue,FILTER_VALIDATE_INT);
    if(!is_int($timeout) || $timeout<5 || $timeout>600)throw new RuntimeException('intelligence_timeout_invalid');
    $body=json_encode($task,JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR);
    if(strlen($body)>8_000_000)throw new RuntimeException('intelligence_request_too_large');
    $context=stream_context_create([
        'http'=>['method'=>'POST','header'=>"Authorization: Bearer ".$token."\r\nContent-Type: application/json\r\nAccept: application/json\r\nConnection: close\r\n",
            'content'=>$body,'timeout'=>$timeout,'ignore_errors'=>true,'follow_location'=>0,'max_redirects'=>0],
        'ssl'=>['verify_peer'=>true,'verify_peer_name'=>true],
    ]);
    $response=@fopen(rtrim($base,'/').'/internal/v1/analyze','rb',false,$context);
    if($response===false)throw new RuntimeException('intelligence_connection_failed');
    try {
        $headers=$http_response_header ?? [];$status=null;$declaredLength=null;
        foreach($headers as $header){
            if(preg_match('/^HTTP\/\S+\s+(\d{3})\b/',$header,$match))$status=(int)$match[1];
            elseif(stripos($header,'Content-Length:')===0){$length=trim(substr($header,15));if(ctype_digit($length))$declaredLength=(int)$length;}
        }
        if($status===null || $status<200 || $status>=300)throw new RuntimeException('intelligence_http_status');
        if($declaredLength!==null && $declaredLength>32_000_000)throw new RuntimeException('intelligence_response_too_large');
        $responseBody=stream_get_contents($response,32_000_001);
        $metadata=stream_get_meta_data($response);
        if(!is_string($responseBody) || strlen($responseBody)>32_000_000 || ($metadata['timed_out'] ?? false))throw new RuntimeException('intelligence_response_limit_or_timeout');
        $decoded=json_decode($responseBody,true,512,JSON_THROW_ON_ERROR);
        if(!is_array($decoded) || ($decoded['tenant_id'] ?? null)!==$tenantId)throw new RuntimeException('intelligence_response_scope_mismatch');
        return [$decoded,'HTTP '.$status];
    } finally {fclose($response);}
}

if ($argc<2 || $argc>5) {fwrite(STDERR,"Usage: php bin/run-news.php COMPANY_ID [--local-cli] [--fixture-feed FILE]\n");exit(2);}
$companyId=$argv[1];$localCli=false;$fixture=null;
for($i=2;$i<$argc;$i++){
    if($argv[$i]==='--local-cli' && !$localCli){$localCli=true;continue;}
    if($argv[$i]==='--fixture-feed' && $fixture===null && isset($argv[$i+1])){$fixture=$argv[++$i];continue;}
    fwrite(STDERR,"Invalid run-news option\n");exit(2);
}
if($fixture!==null && !$localCli){fwrite(STDERR,"--fixture-feed is available only with explicit --local-cli\n");exit(2);}
$deploymentTenantId=getenv('CORE_INTELLIGENCE_TENANT_ID') ?: 'core-local';
if(!is_string($deploymentTenantId) || !preg_match('/^[A-Za-z0-9._:-]{1,128}$/',$deploymentTenantId)){fwrite(STDERR,"Invalid Core Intelligence tenant configuration\n");exit(2);}
$lockPath=dirname(__DIR__).'/var/news-'.$companyId.'.lock';
$lock=fopen($lockPath,'c');if(!$lock || !flock($lock,LOCK_EX|LOCK_NB))exit("News run already active for this company\n");
$db=App\Database::connect();
$q=$db->prepare('SELECT id,synthetic_u,mainokved_name,org_type FROM companies WHERE id=?');$q->execute([$companyId]);$company=$q->fetch();if(!$company)exit("Unknown company\n");
$q=$db->prepare('SELECT revision FROM source_policy_heads WHERE company_id=?');$q->execute([$companyId]);$policyVersion=(int)$q->fetchColumn();if(!$policyVersion)exit("No source policy\n");
$q=$db->prepare('SELECT * FROM news_sources WHERE company_id=? AND enabled=1 ORDER BY id');$q->execute([$companyId]);$sources=$q->fetchAll();if(!$sources)exit("No enabled sources\n");
// Materialize the initial empty profile as a real Core version. Otherwise the
// first confirmed edit would reuse AI's fallback version 1 with different facts.
$db->beginTransaction();
try {
    App\Database::lockCompany($db,$companyId);
    $q=$db->prepare('SELECT version,facts_json FROM company_context_versions WHERE company_id=? ORDER BY version DESC LIMIT 1');$q->execute([$companyId]);$context=$q->fetch();
    if(!$context){
        $db->prepare('INSERT INTO company_context_versions(id,company_id,version,facts_json,source,created_at) VALUES(?,?,?,?,?,?)')->execute([bin2hex(random_bytes(16)),$companyId,1,'{}','initial',gmdate('c')]);
        $context=['version'=>1,'facts_json'=>'{}'];
    }
    $db->commit();
} catch(Throwable $e){$db->rollBack();throw $e;}
$contextVersion=(int)$context['version'];
$confirmed=$context?json_decode($context['facts_json'],true):[];$facts=[];$activities=[];
foreach(($confirmed['activities'] ?? []) as $i=>$value)$activities[]=['activity_id'=>'activity-'.$i,'label'=>$value,'status'=>'confirmed','provenance'=>'Core context version '.$contextVersion];
foreach($confirmed as $key=>$items){if($key==='activities')continue;foreach($items as $i=>$value)$facts[]=['fact_id'=>$key.'-'.$i,'text'=>$value,'provenance'=>'Core context version '.$contextVersion,'status'=>'confirmed'];}
$grants=[];
foreach($sources as $source){$grant=['source_id'=>$source['id'],'source_role'=>$source['source_role'],'data_classification'=>$source['classification'],'uri'=>$source['uri'],'content_type'=>$source['content_type'],'title'=>$source['title'],'publisher'=>$source['publisher'],'language'=>'ru','enabled'=>true,'poll_interval_seconds'=>3600,'timeout_seconds'=>15,'max_bytes'=>2000000,'fetch_articles'=>false,'article_hosts'=>json_decode($source['article_hosts_json'],true) ?: []];if($source['published_at'])$grant['published_at']=$source['published_at'];$grants[]=$grant;}
$runId=bin2hex(random_bytes(16));$now=gmdate('c');
$task=['schema_version'=>'ai-data-task-v1','request_id'=>$runId,'tenant_id'=>$deploymentTenantId,'company_id'=>$companyId,'context_version'=>$contextVersion,'source_policy_version'=>$policyVersion,'operation'=>'collect','trigger'=>'scheduled','knowledge_mode'=>'latest','as_of'=>$now,'source_policy'=>['policy_id'=>'core-'.$companyId,'revision'=>$policyVersion,'tenant_id'=>$deploymentTenantId,'issued_at'=>$now,'sources'=>$grants],'businesses'=>[['company_id'=>$companyId,'context_version'=>$contextVersion,'company'=>['name'=>$company['synthetic_u'],'industry'=>$company['mainokved_name'] ?: '','description'=>$company['org_type'] ?: '','role'=>'','dependencies'=>[],'facts'=>$facts,'activities'=>$activities,'assertions'=>[]]]],'data_classification'=>count(array_filter($sources,fn($s)=>$s['classification']!=='synthetic'))===0?'synthetic':'confidential','synthetic'=>count(array_filter($sources,fn($s)=>$s['classification']!=='synthetic'))===0,'auto_optimize'=>false];
$db->prepare('INSERT INTO news_runs(id,company_id,context_version,policy_version,status,created_at) VALUES(?,?,?,?,?,?)')->execute([$runId,$companyId,$contextVersion,$policyVersion,'running',$now]);
try {
    if($localCli){
        $root=dirname(__DIR__,3);$scratch=dirname(__DIR__).'/var/news';if(!is_dir($scratch))mkdir($scratch,0770,true);
        $taskPath=$scratch.'/'.$runId.'-task.json';$resultPath=$scratch.'/'.$runId.'-result.json';file_put_contents($taskPath,json_encode($task,JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR));
        $python=getenv('INTELLIGENCE_PYTHON') ?: $root.'/apps/intelligence/.venv/bin/python';
        $command=[$python,$root.'/apps/intelligence/scripts/run_core_news.py','--task',$taskPath,'--output',$resultPath,'--state-dir',$root.'/apps/intelligence/.state/core-news/'.$companyId];if($fixture!==null){$command[]='--fixture-feed';$command[]=$fixture;}
        [$code,$stdout,$stderr]=runLocalCli($command,$root);
        if($code!==0)throw new RuntimeException('local_cli_failed');
        $result=json_decode(file_get_contents($resultPath),true,512,JSON_THROW_ON_ERROR);
    } else {
        [$result,$stdout]=postToIntelligence($task,$deploymentTenantId);
    }
} catch(Throwable $e) {
    $db->prepare('UPDATE news_runs SET status=?,error_code=?,finished_at=? WHERE id=?')->execute(['failed',$localCli?'local_cli_failed':'intelligence_http_failed',gmdate('c'),$runId]);
    fwrite(STDERR,$localCli?"Local workflow failed\n":"Intelligence request failed\n");exit(1);
}
if(!is_array($result) || ($result['schema_version'] ?? null)!=='ai-data-result-v1' || ($result['tenant_id'] ?? null)!==$deploymentTenantId || ($result['company_id'] ?? null)!==$companyId || ($result['request_id'] ?? null)!==$runId || !in_array($result['status'] ?? null,['completed','partial','no_change','unknown'],true)){$db->prepare('UPDATE news_runs SET status=?,error_code=?,finished_at=? WHERE id=?')->execute(['failed','invalid_result',gmdate('c'),$runId]);exit("Invalid Intelligence result\n");}
$allowed=array_column($sources,null,'id');$documents=$result['collection']['documents'] ?? [];
$db->beginTransaction();
try {
    // Context/source writers lock the same company row. The network has already
    // finished; only the short publication transaction is serialized here.
    App\Database::lockCompany($db,$companyId);
$q=$db->prepare('SELECT revision FROM source_policy_heads WHERE company_id=?');$q->execute([$companyId]);$currentPolicy=(int)$q->fetchColumn();
$q=$db->prepare('SELECT MAX(version) FROM company_context_versions WHERE company_id=?');$q->execute([$companyId]);$currentContext=(int)$q->fetchColumn() ?: 1;
if($currentPolicy!==$policyVersion || $currentContext!==$contextVersion){$db->rollBack();$db->prepare('UPDATE news_runs SET status=?,error_code=?,finished_at=? WHERE id=?')->execute(['stale','snapshot_changed',gmdate('c'),$runId]);exit("Source policy or context changed during run\n");}
    $added=0;
    foreach($documents as $doc){
        $sourceId=$doc['source_id'] ?? '';if(($doc['tenant_id'] ?? '')!==$deploymentTenantId || !isset($allowed[$sourceId]) || empty($doc['document_revision_ref']) || empty($doc['canonical_uri']) || empty($doc['published_at']) || empty($doc['content_hash']))continue;
        if(!App\Intelligence\SourceUriPolicy::allowsDocumentUri((string)$doc['canonical_uri'],$allowed[$sourceId]))continue;
        $q=$db->prepare('SELECT id FROM news_documents WHERE company_id=? AND source_id=? AND canonical_uri=?');$q->execute([$companyId,$sourceId,$doc['canonical_uri']]);$documentId=$q->fetchColumn();$new=!$documentId;
        if(!$documentId)$documentId=bin2hex(random_bytes(16));
        $excerpt=mb_substr((string)($doc['content'] ?? ''),0,1500);$publisher=mb_substr((string)($doc['publisher'] ?? $allowed[$sourceId]['publisher']),0,240);$title=mb_substr((string)($doc['title'] ?? ''),0,240);
        if($new){$db->prepare('INSERT INTO news_documents(id,company_id,source_id,canonical_uri,title,publisher,author,published_at,fetched_at,latest_revision_ref,latest_hash,excerpt,classification) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)')->execute([$documentId,$companyId,$sourceId,$doc['canonical_uri'],$title,$publisher,$doc['author'] ?? '',$doc['published_at'],$doc['fetched_at'] ?? gmdate('c'),$doc['document_revision_ref'],$doc['content_hash'],$excerpt,$doc['data_classification'] ?? 'confidential']);$added++;}
        else $db->prepare('UPDATE news_documents SET title=?,publisher=?,author=?,published_at=?,fetched_at=?,latest_revision_ref=?,latest_hash=?,excerpt=? WHERE id=?')->execute([$title,$publisher,$doc['author'] ?? '',$doc['published_at'],$doc['fetched_at'] ?? gmdate('c'),$doc['document_revision_ref'],$doc['content_hash'],$excerpt,$documentId]);
        $db->prepare('INSERT INTO news_document_revisions(id,document_id,revision_ref,content_hash,raw_sha256,raw_ref,excerpt,fetched_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(revision_ref) DO NOTHING')->execute([bin2hex(random_bytes(16)),$documentId,$doc['document_revision_ref'],$doc['content_hash'],$doc['raw_sha256'] ?? null,$doc['raw_ref'] ?? null,$excerpt,$doc['fetched_at'] ?? gmdate('c')]);
        if($allowed[$sourceId]['source_role']==='external'){
            $q=$db->prepare('SELECT event_id FROM news_event_documents WHERE document_id=?');$q->execute([$documentId]);$eventId=$q->fetchColumn();
            if(!$eventId){$eventId=bin2hex(random_bytes(16));$db->prepare('INSERT INTO business_events(id,company_id,title,summary,kind,published_on,source_label,source_url,created_at) VALUES(?,?,?,?,?,?,?,?,?)')->execute([$eventId,$companyId,$title,$excerpt,'source_document',substr($doc['published_at'],0,10),$publisher,$doc['canonical_uri'],gmdate('c')]);$db->prepare('INSERT INTO news_event_documents(event_id,document_id) VALUES(?,?)')->execute([$eventId,$documentId]);}
            else $db->prepare('UPDATE business_events SET title=?,summary=?,published_on=?,source_label=?,source_url=? WHERE id=?')->execute([$title,$excerpt,substr($doc['published_at'],0,10),$publisher,$doc['canonical_uri'],$eventId]);
        }
    }
    $metricSql='SELECT d.id,v.title FROM metric_definitions d JOIN metric_versions v ON v.metric_id=d.id AND v.version=(SELECT MAX(v2.version) FROM metric_versions v2 WHERE v2.metric_id=d.id) WHERE d.company_id=? AND d.archived_at IS NULL';
    $q=$db->prepare($metricSql);$q->execute([$companyId]);$metrics=$q->fetchAll();
    $q=$db->prepare('SELECT e.id,e.title,e.summary FROM business_events e JOIN news_event_documents n ON n.event_id=e.id WHERE e.company_id=?');$q->execute([$companyId]);$events=$q->fetchAll();
    foreach($metrics as $metric){
        $terms=preg_split('/[^\p{L}\p{N}]+/u',mb_strtolower($metric['title']));
        $terms=array_filter($terms,fn($term)=>mb_strlen($term)>=5);
        foreach($events as $event){
            $text=mb_strtolower($event['title'].' '.$event['summary']);
            if(!array_filter($terms,fn($term)=>mb_strpos($text,$term)!==false))continue;
            $db->prepare('INSERT INTO metric_event_links(metric_id,event_id,explanation,direction,status,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(metric_id,event_id) DO NOTHING')->execute([$metric['id'],$event['id'],'Topic match only; impact on the metric is not verified.','unknown','proposed',gmdate('c')]);
        }
    }
    $signatures=[];foreach(($result['revisions'] ?? []) as $revision){if(isset($revision['event_id'],$revision['input_signature']))$signatures[$revision['event_id']]=$revision['input_signature'];}
    foreach(($result['candidates'] ?? []) as $candidate){
        $ref=$candidate['versions']['event_document_revision_ref'] ?? null;$pythonEventId=$candidate['versions']['event_id'] ?? null;
        if(!$ref || !$pythonEventId || ($candidate['company_id'] ?? null)!==$companyId)continue;
        $q=$db->prepare('SELECT n.event_id FROM news_document_revisions r JOIN news_event_documents n ON n.document_id=r.document_id JOIN news_documents d ON d.id=r.document_id WHERE r.revision_ref=? AND d.company_id=?');$q->execute([$ref,$companyId]);$eventId=$q->fetchColumn();if(!$eventId)continue;
        $signature=$signatures[$pythonEventId] ?? hash('sha256',$ref.json_encode($candidate['versions']));
        $db->prepare('INSERT INTO news_assessments(id,event_id,input_signature,status,execution_mode,summary,candidate_json,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_id,input_signature) DO NOTHING')->execute([bin2hex(random_bytes(16)),$eventId,$signature,$candidate['status'] ?? 'unknown',$candidate['execution_mode'] ?? 'baseline',$candidate['summary'] ?? '',json_encode($candidate,JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR),gmdate('c')]);
    }
    $db->prepare('UPDATE news_runs SET status=?,result_json=?,finished_at=? WHERE id=?')->execute([$result['status'],json_encode(['coverage'=>$result['coverage'] ?? [],'events'=>$result['events'] ?? [],'candidates'=>$result['candidates'] ?? [],'collection'=>['fetched'=>$result['collection']['fetched'] ?? 0,'errors'=>$result['collection']['errors'] ?? [],'pending_metadata'=>$result['collection']['pending_metadata'] ?? []]],JSON_UNESCAPED_UNICODE),gmdate('c'),$runId]);
    $db->commit();echo json_encode(['run_id'=>$runId,'status'=>$result['status'],'documents_added'=>$added,'documents_seen'=>count($documents),'workflow'=>$stdout],JSON_UNESCAPED_UNICODE),"\n";
} catch(Throwable $e){$db->rollBack();$db->prepare('UPDATE news_runs SET status=?,error_code=?,finished_at=? WHERE id=?')->execute(['failed','import_failed',gmdate('c'),$runId]);fwrite(STDERR,$e->getMessage()."\n");exit(1);}
