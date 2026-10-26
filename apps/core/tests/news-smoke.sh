#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
fixture_dir=$(mktemp -d)
trap 'rm -rf "$fixture_dir"' EXIT
export CORE_SQLITE_PATH="$fixture_dir/core.sqlite"
export CORE_INTELLIGENCE_TENANT_ID="core-local"
php "$project_root/apps/core/bin/schema.php" >/dev/null
php "$project_root/apps/core/bin/seed-demo.php" >/dev/null
company_id=$(php -r 'require $argv[1]; echo App\Database::connect()->query("SELECT id FROM companies WHERE synthetic_u=\"demo-company\"")->fetchColumn();' "$project_root/apps/core/vendor/autoload.php")
php -r '
require $argv[1];$db=App\Database::connect();$company=$argv[2];$user=$db->query("SELECT id FROM app_users LIMIT 1")->fetchColumn();
$db->prepare("INSERT INTO metric_definitions(id,company_id,slug,created_by,created_at) VALUES(?,?,?,?,?)")->execute(["metric-demo",$company,"port",$user,gmdate("c")]);
$db->prepare("INSERT INTO metric_versions(id,metric_id,version,title,description,unit,direction,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)")->execute(["metric-version-demo","metric-demo",1,"Северный порт","Сроки перевозок",null,"neutral",$user,gmdate("c")]);
' "$project_root/apps/core/vendor/autoload.php" "$company_id"
php "$project_root/apps/core/bin/add-news-source.php" "$company_id" https://feeds.example/demo.rss rss external synthetic 'Demo feed' 'Demo source' 2026-10-23T00:00:00+00:00 news.example >/dev/null
php "$project_root/apps/core/bin/run-news.php" "$company_id" --local-cli --fixture-feed "$project_root/apps/intelligence/examples/feeds/demo.rss" >"$fixture_dir/first.json"
php "$project_root/apps/core/bin/run-news.php" "$company_id" --local-cli --fixture-feed "$project_root/apps/intelligence/examples/feeds/demo.rss" >"$fixture_dir/second.json"
php -r '
require $argv[1];$db=App\Database::connect();
foreach(["news_documents"=>2,"news_document_revisions"=>2,"news_assessments"=>2,"news_event_documents"=>2,"metric_event_links"=>1] as $table=>$expected){
 $actual=(int)$db->query("SELECT COUNT(*) FROM ".$table)->fetchColumn();
 if($actual!==$expected){fwrite(STDERR,"$table: expected $expected, got $actual\n");exit(1);}
}
echo "News smoke test passed\n";
' "$project_root/apps/core/vendor/autoload.php"
