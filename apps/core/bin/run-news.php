<?php
require dirname(__DIR__).'/vendor/autoload.php';
if ($argc<2 || $argc>4 || ($argc===4 && $argv[2]!=='--fixture-feed')) {fwrite(STDERR,"Usage: php bin/run-news.php COMPANY_ID [--fixture-feed FILE]\n");exit(2);}
$companyId=$argv[1];$fixture=$argc===4?$argv[3]:null;
$lockPath=dirname(__DIR__).'/var/news-'.$companyId.'.lock';
$lock=fopen($lockPath,'c');if(!$lock || !flock($lock,LOCK_EX|LOCK_NB))exit("News run already active for this company\n");
$db=App\Database::connect();
$q=$db->prepare('SELECT id,synthetic_u,mainokved_name,org_type FROM companies WHERE id=?');$q->execute([$companyId]);$company=$q->fetch();if(!$company)exit("Unknown company\n");
$q=$db->prepare('SELECT revision FROM source_policy_heads WHERE company_id=?');$q->execute([$companyId]);$policyVersion=(int)$q->fetchColumn();if(!$policyVersion)exit("No source policy\n");
$q=$db->prepare('SELECT * FROM news_sources WHERE company_id=? AND enabled=1 ORDER BY id');$q->execute([$companyId]);$sources=$q->fetchAll();if(!$sources)exit("No enabled sources\n");
$q=$db->prepare('SELECT version,facts_json FROM company_context_versions WHERE company_id=? ORDER BY version DESC LIMIT 1');$q->execute([$companyId]);$context=$q->fetch();$contextVersion=$context?(int)$context['version']:1;
$confirmed=$context?json_decode($context['facts_json'],true):[];$facts=[];$activities=[];
foreach(($confirmed['activities'] ?? []) as $i=>$value)$activities[]=['activity_id'=>'activity-'.$i,'label'=>$value,'status'=>'confirmed','provenance'=>'Core context version '.$contextVersion];
foreach($confirmed as $key=>$items){if($key==='activities')continue;foreach($items as $i=>$value)$facts[]=['fact_id'=>$key.'-'.$i,'text'=>$value,'provenance'=>'Core context version '.$contextVersion,'status'=>'confirmed'];}
$grants=[];
foreach($sources as $source){$grant=['source_id'=>$source['id'],'source_role'=>$source['source_role'],'data_classification'=>$source['classification'],'uri'=>$source['uri'],'content_type'=>$source['content_type'],'title'=>$source['title'],'publisher'=>$source['publisher'],'language'=>'ru','enabled'=>true,'poll_interval_seconds'=>3600,'timeout_seconds'=>15,'max_bytes'=>2000000,'fetch_articles'=>false,'article_hosts'=>json_decode($source['article_hosts_json'],true) ?: []];if($source['published_at'])$grant['published_at']=$source['published_at'];$grants[]=$grant;}
$runId=bin2hex(random_bytes(16));$now=gmdate('c');
$task=['schema_version'=>'ai-data-task-v1','request_id'=>$runId,'tenant_id'=>$companyId,'company_id'=>$companyId,'context_version'=>$contextVersion,'source_policy_version'=>$policyVersion,'operation'=>'collect','trigger'=>'scheduled','knowledge_mode'=>'latest','as_of'=>$now,'source_policy'=>['policy_id'=>'core-'.$companyId,'revision'=>$policyVersion,'tenant_id'=>$companyId,'issued_at'=>$now,'sources'=>$grants],'businesses'=>[['company_id'=>$companyId,'context_version'=>$contextVersion,'company'=>['name'=>$company['synthetic_u'],'industry'=>$company['mainokved_name'] ?: '','description'=>$company['org_type'] ?: '','role'=>'','dependencies'=>[],'facts'=>$facts,'activities'=>$activities,'assertions'=>[]]]],'data_classification'=>count(array_filter($sources,fn($s)=>$s['classification']!=='synthetic'))===0?'synthetic':'confidential','synthetic'=>count(array_filter($sources,fn($s)=>$s['classification']!=='synthetic'))===0,'auto_optimize'=>false];
$db->prepare('INSERT INTO news_runs(id,company_id,context_version,policy_version,status,created_at) VALUES(?,?,?,?,?,?)')->execute([$runId,$companyId,$contextVersion,$policyVersion,'running',$now]);
$root=dirname(__DIR__,3);$scratch=dirname(__DIR__).'/var/news';if(!is_dir($scratch))mkdir($scratch,0770,true);
$taskPath=$scratch.'/'.$runId.'-task.json';$resultPath=$scratch.'/'.$runId.'-result.json';file_put_contents($taskPath,json_encode($task,JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR));
$python=getenv('INTELLIGENCE_PYTHON') ?: $root.'/apps/intelligence/.venv/bin/python';
$command=[$python,$root.'/apps/intelligence/scripts/run_core_news.py','--task',$taskPath,'--output',$resultPath,'--state-dir',$root.'/apps/intelligence/.state/core-news/'.$companyId];if($fixture){$command[]='--fixture-feed';$command[]=$fixture;}
$process=proc_open($command,[0=>['pipe','r'],1=>['pipe','w'],2=>['pipe','w']],$pipes,$root);
if(!is_resource($process))exit("Cannot start Python\n");fclose($pipes[0]);$stdout=stream_get_contents($pipes[1]);fclose($pipes[1]);$stderr=stream_get_contents($pipes[2]);fclose($pipes[2]);$code=proc_close($process);
if($code!==0){$db->prepare('UPDATE news_runs SET status=?,error_code=?,finished_at=? WHERE id=?')->execute(['failed','python_failed',gmdate('c'),$runId]);fwrite(STDERR,"Python workflow failed: ".substr($stderr,0,1000)."\n");exit(1);}
$result=json_decode(file_get_contents($resultPath),true);if(!is_array($result) || ($result['tenant_id'] ?? null)!==$companyId || ($result['request_id'] ?? null)!==$runId){$db->prepare('UPDATE news_runs SET status=?,error_code=?,finished_at=? WHERE id=?')->execute(['failed','invalid_result',gmdate('c'),$runId]);exit("Invalid Python result\n");}
$q=$db->prepare('SELECT revision FROM source_policy_heads WHERE company_id=?');$q->execute([$companyId]);$currentPolicy=(int)$q->fetchColumn();
$q=$db->prepare('SELECT MAX(version) FROM company_context_versions WHERE company_id=?');$q->execute([$companyId]);$currentContext=(int)$q->fetchColumn() ?: 1;
if($currentPolicy!==$policyVersion || $currentContext!==$contextVersion){$db->prepare('UPDATE news_runs SET status=?,error_code=?,finished_at=? WHERE id=?')->execute(['stale','snapshot_changed',gmdate('c'),$runId]);exit("Source policy or context changed during run\n");}
$allowed=array_column($sources,null,'id');$documents=$result['collection']['documents'] ?? [];
$db->beginTransaction();
try {
    $added=0;
    foreach($documents as $doc){
        $sourceId=$doc['source_id'] ?? '';if(($doc['tenant_id'] ?? '')!==$companyId || !isset($allowed[$sourceId]) || empty($doc['document_revision_ref']) || empty($doc['canonical_uri']) || empty($doc['published_at']) || empty($doc['content_hash']))continue;
        if(!str_starts_with($doc['canonical_uri'],'https://'))continue;
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
