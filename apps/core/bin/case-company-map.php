<?php
require dirname(__DIR__).'/vendor/autoload.php';
if($argc<3 || $argc>7){fwrite(STDERR,"Usage: php bin/case-company-map.php DATASET_VERSION SYNTHETIC_U [SYNTHETIC_U...]\n");exit(2);}
$db=App\Database::connect();$result=[];
foreach(array_slice($argv,2) as $synthetic){
    $q=$db->prepare('SELECT c.id FROM companies c JOIN import_batches b ON b.id=c.import_batch_id WHERE b.dataset_version=? AND c.synthetic_u=?');$q->execute([$argv[1],$synthetic]);$rows=$q->fetchAll();
    if(count($rows)!==1){fwrite(STDERR,"Unknown or ambiguous company\n");exit(1);}
    $result[$synthetic]=$rows[0]['id'];
}
echo json_encode($result,JSON_THROW_ON_ERROR),"\n";
