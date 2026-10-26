<?php
if (getenv('DATABASE_URL') !== false && getenv('DATABASE_URL') !== '') {
    fwrite(STDERR, "Core tests must not run with DATABASE_URL set; use `docker compose --profile test run --build --rm core-test`.\n");
    exit(2);
}
require dirname(__DIR__).'/vendor/autoload.php';

function check(bool $condition, string $message): void {
    if (!$condition) throw new RuntimeException($message);
}
function runCommand(array $args, string $database, string $stdin = ''): array {
    $process = proc_open([PHP_BINARY, ...$args], [0=>['pipe','r'],1=>['pipe','w'],2=>['pipe','w']], $pipes, dirname(__DIR__), ['CORE_SQLITE_PATH'=>$database]);
    if (!is_resource($process)) throw new RuntimeException('Cannot start PHP');
    fwrite($pipes[0], $stdin); fclose($pipes[0]);
    $out = stream_get_contents($pipes[1]); fclose($pipes[1]);
    $err = stream_get_contents($pipes[2]); fclose($pipes[2]);
    return [proc_close($process), trim($out), trim($err)];
}
function csv(string $file, array $headers, array $rows): void {
    $handle = fopen($file, 'wb');
    fputcsv($handle, $headers, ',', '"', '');
    foreach ($rows as $row) fputcsv($handle, $row, ',', '"', '');
    fclose($handle);
}
$dir = sys_get_temp_dir().'/alfa-core-test-'.bin2hex(random_bytes(5));
mkdir($dir);
try {
    $source=['uri'=>'https://feeds.example/demo.rss','article_hosts_json'=>'["news.example"]'];
    check(App\Intelligence\SourceUriPolicy::allowsDocumentUri('https://news.example/story/1',$source), 'Configured article host was rejected');
    check(App\Intelligence\SourceUriPolicy::allowsDocumentUri('https://feeds.example/demo.rss',$source), 'Source host was rejected');
    check(!App\Intelligence\SourceUriPolicy::allowsDocumentUri('https://other.example/story/1',$source), 'Unconfigured document host was accepted');
    check(!App\Intelligence\SourceUriPolicy::allowsDocumentUri('https://news.example/story/1',['uri'=>'https://feeds.example/demo.rss','article_hosts_json'=>'{}']), 'Malformed article host policy was accepted');
    check(!App\Intelligence\SourceUriPolicy::allowsDocumentUri('https://feeds.example/demo.rss',['uri'=>'https://feeds.example/demo.rss','article_hosts_json'=>'{}']), 'Object host policy was accepted on source host');
    $database = $dir.'/core.sqlite';
    $headers = ['SYNTHETIC_U','ORG_TYPE','OKFS_NAME','COMPANY_AGE_YEARS_GRADATION','ECO_MARK_LABEL','MAINOKVED_CODE','MAINOKVED_NAME','NEGATIVEMARK','AMOUNT_WORKERS_FNS_GRADATION','AMOUNT_WORKERS_SPARK_GRADATION','CL_TYPE','CL_PRODUCT_RKO_MARK','CL_PRODUCT_GAR_MARK','CL_PRODUCT_LOA_MARK','CL_PRODUCT_LEA_MARK','CL_PRODUCT_DEP_MARK','CL_PRODUCT_ZPP_MARK','FTS_FLAG','FTS_COUNTRY_NAME','FACTORING_MARK','REVENUE_COMPANY','GROUP_ID','CITY','REGION'];
    $company = array_fill_keys($headers, '');
    $company['SYNTHETIC_U']='company-1'; $company['MAINOKVED_NAME']='Торговля'; $company['REVENUE_COMPANY']='123';
    csv($dir.'/companies.csv', $headers, [array_values($company)]);
    csv($dir.'/links.csv', ['SYNTHETIC_F','CITY_NAME','RC_NAME','SYNTHETIC_U'], [['person-1','Москва','Центр','company-1']]);
    csv($dir.'/payments.csv', ['BENEFICIARY','PAYER','PAY_AMT','PAY_CNT'], [['company-1','company-1','500','2']]);
    check(runCommand(['bin/schema.php'], $database)[0]===0, 'Schema failed');
    [$status,$output,$error]=runCommand(['bin/import-case.php','v1',$dir.'/companies.csv',$dir.'/links.csv',$dir.'/payments.csv'], $database);
    check($status===0, 'Import failed: '.$error);
    check(json_decode($output,true)['counts']===['companies'=>1,'person_links'=>1,'payments'=>1], 'Wrong import counts');
    putenv('CORE_SQLITE_PATH='.$database);
    $db=App\Database::connect();
    $companyId=$db->query('SELECT id FROM companies')->fetchColumn();
    check($db->query('SELECT revenue_company_raw FROM companies')->fetchColumn()==='123', 'Revenue was modified');
    $fenceRunId=bin2hex(random_bytes(16));
    $db->prepare('INSERT INTO news_runs(id,company_id,context_version,policy_version,status,created_at) VALUES(?,?,?,?,?,?)')->execute([$fenceRunId,$companyId,1,1,'running',gmdate('c')]);
    check(App\NewsRunFence::isActive($db,$companyId,$fenceRunId), 'Active news run did not pass its publication fence');
    $db->prepare("UPDATE news_runs SET status='failed' WHERE id=?")->execute([$fenceRunId]);
    check(!App\NewsRunFence::isActive($db,$companyId,$fenceRunId), 'Expired news run passed its publication fence');
    $db->prepare('DELETE FROM news_runs WHERE id=?')->execute([$fenceRunId]);
    [$status,$userId,$error]=runCommand(['bin/create-user.php','user@example.test','Тест'], $database, "TestPassword12345\n");
    check($status===0, 'User creation failed: '.$error);
    check(password_verify('TestPassword12345', $db->query('SELECT password_hash FROM app_users')->fetchColumn()), 'Password is not hashed');
    check(runCommand(['bin/grant-company.php',$userId,$companyId,'editor'], $database)[0]===0, 'Membership failed');
    check($db->query('SELECT COUNT(*) FROM company_memberships')->fetchColumn()==1, 'Membership missing');
    csv($dir.'/bad-links.csv', ['SYNTHETIC_F','CITY_NAME','RC_NAME','SYNTHETIC_U'], [['person-2','Москва','Центр','missing-company']]);
    check(runCommand(['bin/import-case.php','bad-v1',$dir.'/companies.csv',$dir.'/bad-links.csv'], $database)[0]!==0, 'Bad link accepted');
    check($db->query('SELECT COUNT(*) FROM import_batches')->fetchColumn()==1, 'Failed import was not rolled back');
    App\Controller\AuthController::session();
    $_SESSION=['uid'=>$userId,'seen'=>time()];
    $api=new App\Controller\BusinessController();
    $request=fn(string $method,array $body=[])=>Symfony\Component\HttpFoundation\Request::create('/', $method, [], [], [], ['CONTENT_TYPE'=>'application/json'], json_encode($body,JSON_UNESCAPED_UNICODE));
    $context=$api->saveContext($companyId,$request('PUT',['expectedVersion'=>0,'facts'=>['activities'=>['Торговля']]]));
    check($context->getStatusCode()===201, 'Context save failed: '.$context->getContent());
    $db->prepare('UPDATE company_context_versions SET facts_json=? WHERE company_id=? AND version=1')->execute([json_encode(['activities'=>['Торговля'],'ai_profile'=>['schema_version'=>'company-context-v2','name'=>'Imported profile','dependencies'=>[],'facts'=>[],'activities'=>[],'assertions'=>[]]],JSON_UNESCAPED_UNICODE),$companyId]);
    check($api->saveContext($companyId,$request('PUT',['expectedVersion'=>1,'facts'=>['activities'=>['Другое']]]))->getStatusCode()===201, 'Manual context overlay failed');
    $savedContext=json_decode($api->context($companyId)->getContent(),true)['context'];
    check(($savedContext['version']??null)===2 && ($savedContext['facts']['ai_profile']['name']??null)==='Imported profile' && ($savedContext['facts']['activities'][0]??null)==='Другое', 'Saving manual context discarded the imported profile');
    check($api->saveContext($companyId,$request('PUT',['expectedVersion'=>1,'facts'=>['activities'=>['Другое']]]))->getStatusCode()===409, 'Context conflict not detected');
    $created=$api->createMetric($companyId,$request('POST',['title'=>'Выручка','description'=>'За период','unit'=>'₽','direction'=>'up']));
    check($created->getStatusCode()===201, 'Metric creation failed');
    $metricId=json_decode($created->getContent(),true)['id'];
    check(json_decode($api->metrics($companyId)->getContent(),true)['metrics'][0]['status']==='no_data', 'Missing metric value was invented');
    $badObservation=$api->addObservation($companyId,$metricId,$request('POST',['value'=>'100','unit'=>'USD','periodStart'=>'2026-01-01','periodEnd'=>'2026-01-31','source'=>'Manual']));
    check($badObservation->getStatusCode()===400, 'Mismatched unit accepted');
    $observation=$api->addObservation($companyId,$metricId,$request('POST',['value'=>'100','unit'=>'₽','periodStart'=>'2026-01-01','periodEnd'=>'2026-01-31','source'=>'Manual']));
    check($observation->getStatusCode()===201, 'Observation failed');
    $period=Symfony\Component\HttpFoundation\Request::create('/?from=2026-02-01&to=2026-02-28');
    check(json_decode($api->dashboard($companyId,$period)->getContent(),true)['metrics'][0]['status']==='no_data', 'Period ignored');
    $event=$api->addEvent($companyId,$request('POST',['title'=>'Учебная новость','summary'=>'Проверка','kind'=>'manual','publishedOn'=>'2026-01-20','source'=>'Manual']));
    check($event->getStatusCode()===201, 'Event failed');
    $eventId=json_decode($event->getContent(),true)['id'];
    check($api->linkEvent($companyId,$metricId,$eventId,$request('PUT',['explanation'=>'Возможное влияние','direction'=>'unknown']))->getStatusCode()===200, 'Event link failed');
    check(count(json_decode($api->metricEvents($companyId,$metricId,Symfony\Component\HttpFoundation\Request::create('/'))->getContent(),true)['events'])===1, 'Metric events missing');
    check($api->feedback($companyId,$request('POST',['subjectType'=>'metric','subjectId'=>$metricId,'kind'=>'relevant','comment'=>'Полезно']))->getStatusCode()===201, 'Feedback failed');
    check($api->addDiscussion($companyId,$request('POST',['subjectType'=>'metric','subjectId'=>$metricId,'body'=>'Вопрос']))->getStatusCode()===201, 'Discussion failed');
    $newsApi=new App\Controller\NewsController();
    check($newsApi->startRun($companyId,$request('POST',['maxEvents'=>2]))->getStatusCode()===400, 'News run accepted an unbounded event request');
    $otherId=bin2hex(random_bytes(16));
    $adminId=bin2hex(random_bytes(16));
    $db->prepare('INSERT INTO app_users(id,email,password_hash,display_name,created_at) VALUES(?,?,?,?,?)')->execute([$adminId,'admin@example.test','x','Admin',gmdate('c')]);
    $db->prepare('INSERT INTO company_memberships(user_id,company_id,role,created_at) VALUES(?,?,?,?)')->execute([$adminId,$companyId,'admin',gmdate('c')]);
    $_SESSION=['uid'=>$adminId,'seen'=>time()];
    check($newsApi->startRun($companyId,$request('POST',['maxEvents'=>2]))->getStatusCode()===400, 'Admin could not reach the analysis route validation');
    $db->prepare('INSERT INTO app_users(id,email,password_hash,display_name,created_at) VALUES(?,?,?,?,?)')->execute([$otherId,'viewer@example.test','x','Viewer',gmdate('c')]);
    $db->prepare('INSERT INTO company_memberships(user_id,company_id,role,created_at) VALUES(?,?,?,?)')->execute([$otherId,$companyId,'viewer',gmdate('c')]);
    $_SESSION=['uid'=>$otherId,'seen'=>time()];
    check($api->createMetric($companyId,$request('POST',['title'=>'Запрет','description'=>'Нет прав']))->getStatusCode()===403, 'Viewer could edit');
    check($newsApi->startRun($companyId,$request('POST',[]))->getStatusCode()===403, 'Viewer could start a news run');
    $_SESSION=['uid'=>bin2hex(random_bytes(16)),'seen'=>time()];
    check($api->metrics($companyId)->getStatusCode()===401, 'Unknown user could read');
    echo "Core integration tests passed\n";
} finally {
    foreach (glob($dir.'/*') ?: [] as $file) unlink($file);
    rmdir($dir);
}
