<?php
// Explicit operator setup; never runs as part of normal container startup.
require dirname(__DIR__).'/vendor/autoload.php';
if($argc!==3){fwrite(STDERR,"Usage: php bin/prepare-judging.php /case/core-setup.json USER_EMAIL\n");exit(2);}
try {
    if(!is_file($argv[1]) || filesize($argv[1])>4_000_000)throw new RuntimeException('Invalid setup size');
    $setup=json_decode(file_get_contents($argv[1]),true,512,JSON_THROW_ON_ERROR);
    if(($setup['schema_version'] ?? null)!=='judging-core-setup-v1' || !is_array($setup['profiles'] ?? null) || count($setup['profiles'])>5)throw new RuntimeException('Invalid setup schema');
    $db=App\Database::connect();$q=$db->prepare('SELECT id FROM app_users WHERE email=? AND disabled_at IS NULL');$q->execute([$argv[2]]);$userId=$q->fetchColumn();
    if(!$userId)throw new RuntimeException('Create the review user before setup');
    $report=[];
    foreach($setup['profiles'] as $profile){
        $companyId=$profile['company_id'];$company=$profile['context'];
        if(($company['schema_version'] ?? null)!=='company-context-v2' || !is_array($company['facts'] ?? null))throw new RuntimeException('Invalid prepared company profile');
        $db->beginTransaction();
        try {
            App\Database::lockCompany($db,$companyId);
            $q=$db->prepare('SELECT synthetic_u FROM companies WHERE id=?');$q->execute([$companyId]);
            if($q->fetchColumn()!==$profile['synthetic_u_id'])throw new RuntimeException('Company identifier mismatch');
            $db->prepare('INSERT INTO company_memberships(user_id,company_id,role,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,company_id) DO NOTHING')->execute([$userId,$companyId,'editor',gmdate('c')]);
            $q=$db->prepare('SELECT version,facts_json FROM company_context_versions WHERE company_id=? ORDER BY version DESC LIMIT 1');$q->execute([$companyId]);$prior=$q->fetch();
            $contextStatus='preserved_existing';
            if(!$prior){
                $manual=['activities'=>[],'notes'=>[],'ai_profile'=>$company];
                $db->prepare('INSERT INTO company_context_versions(id,company_id,version,facts_json,source,created_at) VALUES(?,?,?,?,?,?)')->execute([bin2hex(random_bytes(16)),$companyId,1,json_encode($manual,JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR),'organizer_csv',gmdate('c')]);$contextStatus='created';
            }
            $changed=false;$sourceCount=0;
            foreach($setup['sources'] ?? [] as $source){
                if(($source['company_id'] ?? null)!==$companyId)continue;
                if(!preg_match('/^[a-f0-9]{32}$/',$source['source_id'] ?? '') || !in_array($source['source_role'] ?? '',['bank','external'],true) || !in_array($source['classification'] ?? '',['public','confidential'],true))throw new RuntimeException('Invalid source metadata');
                $parts=parse_url($source['uri']);
                if(($parts['scheme'] ?? null)!=='https' || isset($parts['user']) || isset($parts['pass']) || isset($parts['fragment']) || empty($parts['host']))throw new RuntimeException('Invalid source URI');
                $hosts=json_encode($source['article_hosts'] ?? [$parts['host']],JSON_THROW_ON_ERROR);
                $values=[$source['uri'],$source['content_type'],$source['source_role'],$source['classification'],$source['title'],$source['publisher'],$source['published_at'] ?? null,$hosts];
                $q=$db->prepare('SELECT uri,content_type,source_role,classification,title,publisher,published_at,article_hosts_json FROM news_sources WHERE company_id=? AND id=?');$q->execute([$companyId,$source['source_id']]);$existing=$q->fetch(PDO::FETCH_NUM);
                if($existing===false){
                    $db->prepare('INSERT INTO news_sources(id,company_id,uri,content_type,source_role,classification,title,publisher,published_at,article_hosts_json,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)')->execute([$source['source_id'],$companyId,...$values,1,gmdate('c')]);$changed=true;
                }elseif($existing!==$values){
                    $db->prepare('UPDATE news_sources SET uri=?,content_type=?,source_role=?,classification=?,title=?,publisher=?,published_at=?,article_hosts_json=? WHERE company_id=? AND id=?')->execute([...$values,$companyId,$source['source_id']]);$changed=true;
                }
                $sourceCount++;
            }
            if($changed)$db->prepare('INSERT INTO source_policy_heads(company_id,revision,updated_at) VALUES(?,?,?) ON CONFLICT(company_id) DO UPDATE SET revision=source_policy_heads.revision+1,updated_at=excluded.updated_at')->execute([$companyId,1,gmdate('c')]);
            $db->commit();$report[]=['company_id'=>$companyId,'context'=>$contextStatus,'sources'=>$sourceCount,'policy_changed'=>$changed];
        }catch(Throwable $e){$db->rollBack();throw $e;}
    }
    echo json_encode(['companies'=>$report],JSON_UNESCAPED_UNICODE|JSON_THROW_ON_ERROR),"\n";
}catch(Throwable $e){fwrite(STDERR,$e->getMessage()."\n");exit(1);}
