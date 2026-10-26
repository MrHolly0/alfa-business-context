<?php
require dirname(__DIR__).'/vendor/autoload.php';
if ($argc!==3 || !filter_var($argv[1],FILTER_VALIDATE_EMAIL)) {fwrite(STDERR,"Usage: php bin/create-user.php email display-name < password.txt\n");exit(2);}
$password=rtrim(stream_get_contents(STDIN),"\r\n");
if(strlen($password)<12 || strlen($password)>1024){fwrite(STDERR,"Password must have 12-1024 bytes\n");exit(2);}
$id=bin2hex(random_bytes(16));
App\Database::connect()->prepare('INSERT INTO app_users(id,email,password_hash,display_name,created_at) VALUES(?,?,?,?,?)')->execute([$id,strtolower($argv[1]),password_hash($password,PASSWORD_DEFAULT),$argv[2],gmdate('c')]);
echo $id,"\n";
