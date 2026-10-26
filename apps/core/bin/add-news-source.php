<?php
require dirname(__DIR__).'/vendor/autoload.php';
if ($argc<8 || $argc>10) {fwrite(STDERR,"Usage: php bin/add-news-source.php COMPANY_ID HTTPS_URL rss|atom|html|pdf external|bank public|synthetic|confidential TITLE PUBLISHER [PUBLISHED_AT] [ARTICLE_HOSTS_CSV]\n");exit(2);}
[$companyId,$uri,$type,$role,$classification,$title,$publisher]=array_slice($argv,1,7);
$types=['rss'=>'application/rss+xml','atom'=>'application/atom+xml','html'=>'text/html','pdf'=>'application/pdf'];
if (!isset($types[$type]) || !in_array($role,['external','bank'],true) || !in_array($classification,['public','synthetic','confidential'],true) || !filter_var($uri,FILTER_VALIDATE_URL) || parse_url($uri,PHP_URL_SCHEME)!=='https' || trim($title)==='' || trim($publisher)==='') exit("Invalid source\n");
$published=$argv[8] ?? null;
if ($published!==null && !DateTimeImmutable::createFromFormat(DateTimeInterface::ATOM,$published)) exit("Invalid publication time\n");
$hosts=$argv[9] ?? parse_url($uri,PHP_URL_HOST);
$hostList=array_values(array_unique(array_filter(array_map('trim',explode(',',$hosts)))));
if (!$hostList || count($hostList)>5 || array_filter($hostList,fn($h)=>!preg_match('/^[a-z0-9.-]+$/i',$h))) exit("Invalid article hosts\n");
$hostsJson=json_encode($hostList,JSON_THROW_ON_ERROR);
$db=App\Database::connect();$db->beginTransaction();
try {
    App\Database::lockCompany($db,$companyId);
    $q=$db->prepare('SELECT id FROM companies WHERE id=?');$q->execute([$companyId]);if(!$q->fetchColumn())throw new RuntimeException('Unknown company');
    $q=$db->prepare('SELECT id FROM news_sources WHERE company_id=? AND uri=?');$q->execute([$companyId,$uri]);$id=$q->fetchColumn();
    if ($id) $db->prepare('UPDATE news_sources SET content_type=?,source_role=?,classification=?,title=?,publisher=?,published_at=?,article_hosts_json=?,enabled=1 WHERE id=?')->execute([$types[$type],$role,$classification,$title,$publisher,$published,$hostsJson,$id]);
    else {$id=bin2hex(random_bytes(16));$db->prepare('INSERT INTO news_sources(id,company_id,uri,content_type,source_role,classification,title,publisher,published_at,article_hosts_json,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)')->execute([$id,$companyId,$uri,$types[$type],$role,$classification,$title,$publisher,$published,$hostsJson,1,gmdate('c')]);}
    $db->prepare('INSERT INTO source_policy_heads(company_id,revision,updated_at) VALUES(?,?,?) ON CONFLICT(company_id) DO UPDATE SET revision=source_policy_heads.revision+1,updated_at=excluded.updated_at')->execute([$companyId,1,gmdate('c')]);
    $db->commit();echo $id,"\n";
} catch(Throwable $e){$db->rollBack();fwrite(STDERR,$e->getMessage()."\n");exit(1);}
