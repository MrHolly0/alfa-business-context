<?php
require dirname(__DIR__).'/vendor/autoload.php';
$db=App\Database::connect();
$sql=file_get_contents(dirname(__DIR__).'/schema.sql');
$db->exec($sql);
$driver=$db->getAttribute(PDO::ATTR_DRIVER_NAME);
$columns=$driver==='sqlite' ? $db->query("PRAGMA table_info(news_sources)")->fetchAll() : $db->query("SELECT column_name AS name FROM information_schema.columns WHERE table_name='news_sources'")->fetchAll();
$names=array_map(fn($row)=>$row[$driver==='sqlite'?'name':'name'],$columns);
if (!in_array('article_hosts_json',$names,true)) $db->exec("ALTER TABLE news_sources ADD COLUMN article_hosts_json TEXT NOT NULL DEFAULT '[]'");
echo "Schema ready\n";
