with (import <nixpkgs>) {};
(python27.withPackages (ps: [
    ps.whoosh
    ps.wxPython
])).env
