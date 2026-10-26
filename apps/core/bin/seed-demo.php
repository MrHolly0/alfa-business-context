<?php
require dirname(__DIR__).'/vendor/autoload.php';
$db=App\Database::connect();
$db->beginTransaction();
try {
    $email='demo@alpha.test';
    $password='DemoAlpha2026!';
    $q=$db->prepare('SELECT id FROM app_users WHERE email=?');
    $q->execute([$email]);
    $userId=$q->fetchColumn();
    if (!$userId) {
        $userId=bin2hex(random_bytes(16));
        $db->prepare('INSERT INTO app_users(id,email,password_hash,display_name,created_at) VALUES(?,?,?,?,?)')->execute([$userId,$email,password_hash($password,PASSWORD_DEFAULT),'Анна',gmdate('c')]);
    }
    $q=$db->prepare('SELECT id FROM import_batches WHERE dataset_version=?');
    $q->execute(['demo-v1']);
    $batchId=$q->fetchColumn();
    if (!$batchId) {
        $batchId=bin2hex(random_bytes(16));
        $db->prepare('INSERT INTO import_batches(id,dataset_version,source_name,sha256,imported_at) VALUES(?,?,?,?,?)')->execute([$batchId,'demo-v1','seed-demo.php',str_repeat('0',64),gmdate('c')]);
    }
    $q=$db->prepare('SELECT id FROM companies WHERE import_batch_id=? AND synthetic_u=?');
    $q->execute([$batchId,'demo-company']);
    $companyId=$q->fetchColumn();
    if (!$companyId) {
        $companyId=bin2hex(random_bytes(16));
        $raw=json_encode(['SYNTHETIC_U'=>'demo-company','ORG_TYPE'=>'ООО','MAINOKVED_NAME'=>'Тестовая компания','CITY'=>'Москва','REGION'=>'Москва'],JSON_UNESCAPED_UNICODE);
        $db->prepare('INSERT INTO companies(id,import_batch_id,synthetic_u,org_type,mainokved_name,city,region,source_row,raw_json,raw_hash) VALUES(?,?,?,?,?,?,?,?,?,?)')->execute([$companyId,$batchId,'demo-company','ООО','Тестовая компания','Москва','Москва',1,$raw,hash('sha256',$raw)]);
    }
    $db->prepare('INSERT INTO company_memberships(user_id,company_id,role,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,company_id) DO UPDATE SET role=excluded.role')->execute([$userId,$companyId,'admin',gmdate('c')]);
    $db->commit();
    echo "Demo user ready: $email / $password\n";
} catch (Throwable $e) {
    $db->rollBack();
    throw $e;
}
