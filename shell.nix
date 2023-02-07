let
     pkgs = import (builtins.fetchGit {
         name = "python2-7-17";                                                 
         url = "https://github.com/NixOS/nixpkgs/";                       
         ref = "refs/heads/nixpkgs-unstable";                     
         rev = "2d9888f61c80f28b09d64f5e39d0ba02e3923057";                                           
     }) {};                                                                           
     python27 = pkgs.python27Full;
in
    (python27.withPackages (ps: [
        ps.whoosh
        ps.wxPython
    ])).env
