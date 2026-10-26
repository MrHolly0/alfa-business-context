<?php
require dirname(__DIR__).'/vendor/autoload.php';
$db=App\Database::connect();
$companies=$db->query('SELECT DISTINCT company_id FROM news_sources WHERE enabled=1 ORDER BY company_id')->fetchAll(PDO::FETCH_COLUMN);
$root=dirname(__DIR__);
foreach($companies as $companyId){
    $q=$db->prepare('SELECT created_at FROM news_runs WHERE company_id=? ORDER BY created_at DESC LIMIT 1');$q->execute([$companyId]);$last=$q->fetchColumn();
    if($last && time()-strtotime($last)<3600)continue;
    $process=proc_open([PHP_BINARY,$root.'/bin/run-news.php',$companyId],[0=>['pipe','r'],1=>['pipe','w'],2=>['pipe','w']],$pipes,$root);
    if(!is_resource($process)){fwrite(STDERR,"Cannot start $companyId\n");continue;}
    fclose($pipes[0]);$out=stream_get_contents($pipes[1]);fclose($pipes[1]);$err=stream_get_contents($pipes[2]);fclose($pipes[2]);$code=proc_close($process);
    if($code===0)echo $out;else fwrite(STDERR,"$companyId: ".substr($err,0,500)."\n");
}
