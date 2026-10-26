<?php
require dirname(__DIR__).'/vendor/autoload.php';
if ($argc<3 || $argc>5) {fwrite(STDERR,"Usage: php bin/import-case.php VERSION companies.csv [person-links.csv] [payments.csv]\n");exit(2);}
$version=$argv[1];if(!preg_match('/^[a-zA-Z0-9._-]{1,100}$/',$version)) exit("Invalid version\n");
$fields=['SYNTHETIC_U','ORG_TYPE','OKFS_NAME','COMPANY_AGE_YEARS_GRADATION','ECO_MARK_LABEL','MAINOKVED_CODE','MAINOKVED_NAME','NEGATIVEMARK','AMOUNT_WORKERS_FNS_GRADATION','AMOUNT_WORKERS_SPARK_GRADATION','CL_TYPE','CL_PRODUCT_RKO_MARK','CL_PRODUCT_GAR_MARK','CL_PRODUCT_LOA_MARK','CL_PRODUCT_LEA_MARK','CL_PRODUCT_DEP_MARK','CL_PRODUCT_ZPP_MARK','FTS_FLAG','FTS_COUNTRY_NAME','FACTORING_MARK','REVENUE_COMPANY','GROUP_ID','CITY','REGION'];
function rows(string $file,array $required): Generator {
    $f=fopen($file,'rb');if(!$f)throw new RuntimeException('Cannot open '.$file);
    $header=fgetcsv($f, 0, ",", '"', '');if(!$header)throw new RuntimeException('Empty CSV');
    $header=array_map(fn($x)=>trim($x,"\xEF\xBB\xBF \t\r\n"),$header);
    if(count($header)!==count(array_unique($header)))throw new RuntimeException('Duplicate columns');
    foreach($required as $key)if(!in_array($key,$header,true))throw new RuntimeException('Missing column '.$key);
    $line=1;while(($v=fgetcsv($f, 0, ",", '"', ''))!==false){$line++;if(count($v)!==count($header))throw new RuntimeException("Column count at row $line");$row=array_combine($header,$v);yield [$line,$row];}
    fclose($f);
}
function uid(): string {return bin2hex(random_bytes(16));}
function raw(array $r): string {return json_encode($r,JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR);}
$db=App\Database::connect();$db->beginTransaction();
try {
    $batch=uid();$db->prepare('INSERT INTO import_batches(id,dataset_version,source_name,sha256,imported_at) VALUES(?,?,?,?,?)')->execute([$batch,$version,basename($argv[2]),hash_file('sha256',$argv[2]),gmdate('c')]);
    $cols=['id','import_batch_id','synthetic_u'];foreach(array_slice($fields,1) as $f)$cols[]=strtolower($f)==='revenue_company'?'revenue_company_raw':strtolower($f);array_push($cols,'source_row','raw_json','raw_hash');
    $stmt=$db->prepare('INSERT INTO companies('.implode(',',$cols).') VALUES('.implode(',',array_fill(0,count($cols),'?')).')');$companies=[];$counts=['companies'=>0,'person_links'=>0,'payments'=>0];
    foreach(rows($argv[2],$fields) as [$line,$r]){$u=trim($r['SYNTHETIC_U']);if($u==='')throw new RuntimeException("Missing SYNTHETIC_U at row $line");$companies[$u]=true;$json=raw($r);$params=[uid(),$batch,$u];foreach(array_slice($fields,1) as $f)$params[]=$r[$f];array_push($params,$line,$json,hash('sha256',$json));$stmt->execute($params);$counts['companies']++;}
    if(isset($argv[3])){
        $q=$db->prepare('INSERT INTO person_company_links(id,import_batch_id,synthetic_f,synthetic_u,city_name,rc_name,source_row,raw_json,raw_hash) VALUES(?,?,?,?,?,?,?,?,?)');
        foreach(rows($argv[3],['SYNTHETIC_F','CITY_NAME','RC_NAME','SYNTHETIC_U']) as [$line,$r]){if(!isset($companies[$r['SYNTHETIC_U']]))throw new RuntimeException("Unknown company at person row $line");$json=raw($r);$q->execute([uid(),$batch,$r['SYNTHETIC_F'],$r['SYNTHETIC_U'],$r['CITY_NAME'],$r['RC_NAME'],$line,$json,hash('sha256',$json)]);$counts['person_links']++;}
    }
    if(isset($argv[4])){
        $q=$db->prepare('INSERT INTO payment_aggregates(id,import_batch_id,beneficiary,payer,pay_amt_raw,pay_cnt_raw,source_row,raw_json,raw_hash) VALUES(?,?,?,?,?,?,?,?,?)');
        foreach(rows($argv[4],['BENEFICIARY','PAYER','PAY_AMT','PAY_CNT']) as [$line,$r]){if(!isset($companies[$r['BENEFICIARY']],$companies[$r['PAYER']]))throw new RuntimeException("Unknown company at payment row $line");$json=raw($r);$q->execute([uid(),$batch,$r['BENEFICIARY'],$r['PAYER'],$r['PAY_AMT'],$r['PAY_CNT'],$line,$json,hash('sha256',$json)]);$counts['payments']++;}
    }
    $db->commit();echo json_encode(['batch_id'=>$batch,'counts'=>$counts],JSON_UNESCAPED_UNICODE),"\n";
}catch(Throwable $e){$db->rollBack();fwrite(STDERR,$e->getMessage()."\n");exit(1);}
