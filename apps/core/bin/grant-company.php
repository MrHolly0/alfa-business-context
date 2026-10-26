<?php
require dirname(__DIR__).'/vendor/autoload.php';
if($argc!==4 || !in_array($argv[3],['viewer','editor','admin'],true)){fwrite(STDERR,"Usage: php bin/grant-company.php user-id company-id viewer|editor|admin\n");exit(2);}
App\Database::connect()->prepare('INSERT INTO company_memberships(user_id,company_id,role,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,company_id) DO UPDATE SET role=excluded.role')->execute([$argv[1],$argv[2],$argv[3],gmdate('c')]);
echo "Granted\n";
