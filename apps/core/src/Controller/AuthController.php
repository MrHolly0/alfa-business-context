<?php
namespace App\Controller;
use App\Database;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\Routing\Attribute\Route;
final class AuthController {
    public static function session(): void {
        if (session_status() === PHP_SESSION_ACTIVE) return;
        session_name('alfa_core');
        session_set_cookie_params(['httponly'=>true,'secure'=>!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS']!=='off','samesite'=>'Strict','path'=>'/']);
        session_start();
    }
    public static function user(): ?array {
        self::session();
        if (!isset($_SESSION['uid']) || (time()-($_SESSION['seen'] ?? 0))>1800) { unset($_SESSION['uid']); return null; }
        $_SESSION['seen']=time();
        $q=Database::connect()->prepare('SELECT id,email,display_name FROM app_users WHERE id=? AND disabled_at IS NULL');
        $q->execute([$_SESSION['uid']]);
        return $q->fetch() ?: null;
    }
    public static function sameOrigin(Request $request): bool {
        $origin=$request->headers->get('Origin');
        if (!$origin) return true; // Non-browser callers; cookie remains SameSite=Strict.
        $parts=parse_url($origin);
        return $parts && ($parts['host'] ?? '')===$request->getHost() && ($parts['scheme'] ?? '')===$request->getScheme() && ($parts['port'] ?? ($request->getScheme()==='https'?443:80))===$request->getPort();
    }
    #[Route('/api/auth/login',methods:['POST'])]
    public function login(Request $request): JsonResponse {
        if (!self::sameOrigin($request)) return new JsonResponse(['error'=>'Bad origin'],403);
        $input=json_decode($request->getContent(),true);
        if (!is_array($input)) return new JsonResponse(['error'=>'Invalid JSON'],400);
        $email=strtolower(trim((string)($input['email'] ?? '')));
        $password=(string)($input['password'] ?? '');
        if (!filter_var($email,FILTER_VALIDATE_EMAIL) || strlen($password)>1024) return new JsonResponse(['error'=>'Invalid credentials'],401);
        $q=Database::connect()->prepare('SELECT id,email,display_name,password_hash FROM app_users WHERE email=? AND disabled_at IS NULL');
        $q->execute([$email]);$user=$q->fetch();
        if (!$user || !password_verify($password,$user['password_hash'])) return new JsonResponse(['error'=>'Invalid credentials'],401);
        self::session();session_regenerate_id(true);$_SESSION=['uid'=>$user['id'],'seen'=>time()];
        return new JsonResponse(['user'=>['id'=>$user['id'],'email'=>$user['email'],'displayName'=>$user['display_name']]]);
    }
    #[Route('/api/auth/me',methods:['GET'])]
    public function me(): JsonResponse { $user=self::user();return $user?new JsonResponse(['user'=>$user]):new JsonResponse(['error'=>'Unauthenticated'],401); }
    #[Route('/api/auth/logout',methods:['POST'])]
    public function logout(Request $request): JsonResponse {
        if (!self::sameOrigin($request)) return new JsonResponse(['error'=>'Bad origin'],403);
        self::session();$_SESSION=[];session_regenerate_id(true);return new JsonResponse(['ok'=>true]);
    }
}
