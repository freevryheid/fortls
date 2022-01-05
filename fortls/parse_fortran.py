from __future__ import print_function, annotations

import hashlib
import logging
import os
import re
import sys
from collections import namedtuple

from fortls.constants import (
    DO_TYPE_ID,
    INTERFACE_TYPE_ID,
    PY3K,
    SELECT_TYPE_ID,
    SUBMODULE_TYPE_ID,
)
from fortls.helper_functions import (
    detect_fixed_format,
    find_paren_match,
    find_word_in_line,
    get_paren_level,
    get_paren_substring,
    map_keywords,
    separate_def_list,
    strip_line_label,
    strip_strings,
)
from fortls.objects import (
    fortran_associate,
    fortran_ast,
    fortran_block,
    fortran_do,
    fortran_enum,
    fortran_function,
    fortran_if,
    fortran_int,
    fortran_meth,
    fortran_module,
    fortran_program,
    fortran_scope,
    fortran_select,
    fortran_submodule,
    fortran_subroutine,
    fortran_type,
    fortran_var,
    fortran_where,
)
from fortls.regex_patterns import (
    ASSOCIATE_REGEX,
    BLOCK_REGEX,
    CALL_REGEX,
    CONTAINS_REGEX,
    DEFINED_REGEX,
    DO_REGEX,
    END_ASSOCIATE_REGEX,
    END_BLOCK_REGEX,
    END_DO_REGEX,
    END_ENUMD_REGEX,
    END_FUN_REGEX,
    END_IF_REGEX,
    END_INT_REGEX,
    END_MOD_REGEX,
    END_PRO_REGEX,
    END_PROG_REGEX,
    END_REGEX,
    END_SELECT_REGEX,
    END_SMOD_REGEX,
    END_SUB_REGEX,
    END_TYPED_REGEX,
    END_WHERE_REGEX,
    END_WORD_REGEX,
    ENUM_DEF_REGEX,
    EXTENDS_REGEX,
    FIXED_COMMENT_LINE_MATCH,
    FIXED_CONT_REGEX,
    FIXED_DOC_MATCH,
    FIXED_OPENMP_MATCH,
    FREE_COMMENT_LINE_MATCH,
    FREE_CONT_REGEX,
    FREE_DOC_MATCH,
    FREE_OPENMP_MATCH,
    FUN_REGEX,
    GEN_ASSIGN_REGEX,
    GENERIC_PRO_REGEX,
    IF_REGEX,
    IMPLICIT_REGEX,
    IMPORT_REGEX,
    INCLUDE_REGEX,
    INT_REGEX,
    INT_STMNT_REGEX,
    KEYWORD_LIST_REGEX,
    KIND_SPEC_REGEX,
    MOD_REGEX,
    NAT_VAR_REGEX,
    NON_DEF_REGEX,
    PARAMETER_VAL_REGEX,
    PP_DEF_REGEX,
    PP_INCLUDE_REGEX,
    PP_REGEX,
    PRO_LINK_REGEX,
    PROCEDURE_STMNT_REGEX,
    PROG_REGEX,
    RESULT_REGEX,
    SCOPE_DEF_REGEX,
    SELECT_DEFAULT_REGEX,
    SELECT_REGEX,
    SELECT_TYPE_REGEX,
    SUB_MOD_REGEX,
    SUB_PAREN_MATCH,
    SUB_REGEX,
    SUBMOD_REGEX,
    TATTR_LIST_REGEX,
    THEN_REGEX,
    TYPE_DEF_REGEX,
    TYPE_STMNT_REGEX,
    USE_REGEX,
    VIS_REGEX,
    WHERE_REGEX,
    WORD_REGEX,
)

if not PY3K:
    import io

# Helper types
VAR_info = namedtuple("VAR_info", ["type_word", "keywords", "var_names"])
SUB_info = namedtuple("SUB_info", ["name", "args", "mod_flag", "keywords"])
FUN_info = namedtuple(
    "FUN_info", ["name", "args", "return_type", "return_var", "mod_flag", "keywords"]
)
SELECT_info = namedtuple("SELECT_info", ["type", "binding", "desc"])
CLASS_info = namedtuple("CLASS_info", ["name", "parent", "keywords"])
USE_info = namedtuple("USE_info", ["mod_name", "only_list", "rename_map"])
GEN_info = namedtuple("GEN_info", ["bound_name", "pro_links", "vis_flag"])
SMOD_info = namedtuple("SMOD_info", ["name", "parent"])
INT_info = namedtuple("INT_info", ["name", "abstract"])
VIS_info = namedtuple("VIS_info", ["type", "obj_names"])


log = logging.getLogger(__name__)


def get_line_context(line: str) -> tuple[str, None]:
    """Get context of ending position in line (for completion)

    Parameters
    ----------
    line : str
        file line

    Returns
    -------
    tuple[str, None]
        Possible string values:
        `var_key`, `pro_line`, `var_only`, `mod_mems`, `mod_only`, `pro_link`,
        `skip`, `import`, `vis`, `call`, `type_only`, `int_only`, `first`, `default`
    """
    last_level, sections = get_paren_level(line)
    lev1_end = sections[-1][1]
    # Test if variable definition statement
    test_match = read_var_def(line)
    if test_match is not None:
        if test_match[0] == "var":
            if (test_match[1].var_names is None) and (lev1_end == len(line)):
                return "var_key", None
            # Procedure link?
            if (test_match[1].type_word == "PROCEDURE") and (line.find("=>") > 0):
                return "pro_link", None
            return "var_only", None
    # Test if in USE statement
    test_match = read_use_stmt(line)
    if test_match is not None:
        if len(test_match[1].only_list) > 0:
            return "mod_mems", test_match[1].mod_name
        else:
            return "mod_only", None
    # Test for interface procedure link
    if PRO_LINK_REGEX.match(line):
        return "pro_link", None
    # Test if scope declaration or end statement (no completion provided)
    if SCOPE_DEF_REGEX.match(line) or END_REGEX.match(line):
        return "skip", None
    # Test if import statement
    if IMPORT_REGEX.match(line):
        return "import", None
    # Test if visibility statement
    if VIS_REGEX.match(line):
        return "vis", None
    # In type-def
    type_def = False
    if TYPE_DEF_REGEX.match(line) is not None:
        type_def = True
    # Test if in call statement
    if lev1_end == len(line):
        if CALL_REGEX.match(last_level) is not None:
            return "call", None
    # Test if variable definition using type/class or procedure
    if (len(sections) == 1) and (sections[0][0] >= 1):
        # Get string one level up
        test_str, _ = get_paren_level(line[: sections[0][0] - 1])
        if (TYPE_STMNT_REGEX.match(test_str) is not None) or (
            type_def and EXTENDS_REGEX.search(test_str) is not None
        ):
            return "type_only", None
        if PROCEDURE_STMNT_REGEX.match(test_str) is not None:
            return "int_only", None
    # Only thing on line?
    if INT_STMNT_REGEX.match(line) is not None:
        return "first", None
    # Default or skip context
    if type_def:
        return "skip", None
    else:
        return "default", None


def parse_var_keywords(test_str: str) -> tuple[list[str], str]:
    """Parse Fortran variable declaration keywords"""
    keyword_match = KEYWORD_LIST_REGEX.match(test_str)
    keywords = []
    while keyword_match is not None:
        tmp_str = re.sub(r"^[, ]*", "", keyword_match.group(0))
        test_str = test_str[keyword_match.end(0) :]
        if tmp_str.lower().startswith("dimension"):
            match_char = find_paren_match(test_str)
            if match_char < 0:
                break  # Incomplete dimension statement
            else:
                tmp_str += test_str[: match_char + 1]
                test_str = test_str[match_char + 1 :]
        tmp_str = re.sub(r"^[, ]*", "", tmp_str)
        keywords.append(tmp_str.strip().upper())
        keyword_match = KEYWORD_LIST_REGEX.match(test_str)
    return keywords, test_str


def read_var_def(line, type_word=None, fun_only=False):
    """Attempt to read variable definition line"""
    if type_word is None:
        type_match = NAT_VAR_REGEX.match(line)
        if type_match is None:
            return None
        else:
            type_word = type_match.group(0).strip()
            trailing_line = line[type_match.end(0) :]
    else:
        trailing_line = line[len(type_word) :]
    type_word = type_word.upper()
    trailing_line = trailing_line.split("!")[0]
    if len(trailing_line) == 0:
        return None
    #
    kind_match = KIND_SPEC_REGEX.match(trailing_line)
    if kind_match is not None:
        kind_str = kind_match.group(1).replace(" ", "")
        type_word += kind_str
        trailing_line = trailing_line[kind_match.end(0) :]
        if kind_str.find("(") >= 0:
            match_char = find_paren_match(trailing_line)
            if match_char < 0:
                return None  # Incomplete type spec
            else:
                kind_word = trailing_line[: match_char + 1].strip()
                type_word += kind_word
                trailing_line = trailing_line[match_char + 1 :]
    else:
        # Class and Type statements need a kind spec
        if type_word in ("TYPE", "CLASS"):
            return None
        # Make sure next character is space or comma or colon
        if not trailing_line[0] in (" ", ",", ":"):
            return None
    #
    keywords, trailing_line = parse_var_keywords(trailing_line)
    # Check if function
    fun_def = read_fun_def(trailing_line, [type_word, keywords])
    if (fun_def is not None) or fun_only:
        return fun_def
    #
    line_split = trailing_line.split("::")
    if len(line_split) == 1:
        if len(keywords) > 0:
            var_words = None
        else:
            trailing_line = line_split[0]
            var_words = separate_def_list(trailing_line.strip())
    else:
        trailing_line = line_split[1]
        var_words = separate_def_list(trailing_line.strip())
        if var_words is None:
            var_words = []
    #
    return "var", VAR_info(type_word, keywords, var_words)


def read_fun_def(line, return_type=None, mod_flag=False):
    """Attempt to read FUNCTION definition line"""
    mod_match = SUB_MOD_REGEX.match(line)
    mods_found = False
    keywords = []
    while mod_match is not None:
        mods_found = True
        line = line[mod_match.end(0) :]
        keywords.append(mod_match.group(1))
        mod_match = SUB_MOD_REGEX.match(line)
    if mods_found:
        tmp_var = read_var_def(line, fun_only=True)
        if tmp_var is not None:
            return tmp_var
    fun_match = FUN_REGEX.match(line)
    if fun_match is None:
        return None
    #
    name = fun_match.group(1)
    trailing_line = line[fun_match.end(0) :].split("!")[0]
    trailing_line = trailing_line.strip()
    #
    paren_match = SUB_PAREN_MATCH.match(trailing_line)
    args = ""
    if paren_match is not None:
        word_match = WORD_REGEX.findall(paren_match.group(0))
        if word_match is not None:
            word_match = [word for word in word_match]
            args = ",".join(word_match)
        trailing_line = trailing_line[paren_match.end(0) :]
    #
    return_var = None
    if return_type is None:
        trailing_line = trailing_line.strip()
        results_match = RESULT_REGEX.match(trailing_line)
        if results_match is not None:
            return_var = results_match.group(1).strip().lower()
    return "fun", FUN_info(name, args, return_type, return_var, mod_flag, keywords)


def read_sub_def(line: str, mod_flag=False):
    """Attempt to read SUBROUTINE definition line"""
    keywords = []
    mod_match = SUB_MOD_REGEX.match(line)
    while mod_match is not None:
        line = line[mod_match.end(0) :]
        keywords.append(mod_match.group(1))
        mod_match = SUB_MOD_REGEX.match(line)
    sub_match = SUB_REGEX.match(line)
    if sub_match is None:
        return None
    #
    name = sub_match.group(1)
    trailing_line = line[sub_match.end(0) :].split("!")[0]
    trailing_line = trailing_line.strip()
    #
    paren_match = SUB_PAREN_MATCH.match(trailing_line)
    args = ""
    if paren_match is not None:
        word_match = WORD_REGEX.findall(paren_match.group(0))
        if word_match is not None:
            word_match = [word for word in word_match]
            args = ",".join(word_match)
        trailing_line = trailing_line[paren_match.end(0) :]
    return "sub", SUB_info(name, args, mod_flag, keywords)


def read_block_def(line):
    """Attempt to read BLOCK definition line"""
    block_match = BLOCK_REGEX.match(line)
    if block_match is not None:
        name = block_match.group(1)
        if name is not None:
            name = name.replace(":", " ").strip()
        return "block", name
    #
    line_stripped = strip_strings(line, maintain_len=True)
    line_no_comment = line_stripped.split("!")[0].rstrip()
    do_match = DO_REGEX.match(line_no_comment)
    if do_match is not None:
        return "do", do_match.group(1).strip()
    #
    where_match = WHERE_REGEX.match(line_no_comment)
    if where_match is not None:
        trailing_line = line[where_match.end(0) :]
        close_paren = find_paren_match(trailing_line)
        if close_paren < 0:
            return "where", True
        if WORD_REGEX.match(trailing_line[close_paren + 1 :].strip()):
            return "where", True
        else:
            return "where", False
    #
    if_match = IF_REGEX.match(line_no_comment)
    if if_match is not None:
        then_match = THEN_REGEX.search(line_no_comment)
        if then_match is not None:
            return "if", None
    return None


def read_associate_def(line):
    assoc_match = ASSOCIATE_REGEX.match(line)
    if assoc_match is not None:
        trailing_line = line[assoc_match.end(0) :]
        match_char = find_paren_match(trailing_line)
        if match_char < 0:
            return "assoc", []
        var_words = separate_def_list(trailing_line[:match_char].strip())
        return "assoc", var_words


def read_select_def(line):
    """Attempt to read SELECT definition line"""
    select_match = SELECT_REGEX.match(line)
    select_desc = None
    select_binding = None
    if select_match is None:
        select_type_match = SELECT_TYPE_REGEX.match(line)
        if select_type_match is None:
            select_default_match = SELECT_DEFAULT_REGEX.match(line)
            if select_default_match is None:
                return None
            else:
                return "select", SELECT_info(4, None, None)
        select_type = 3
        select_desc = select_type_match.group(1).upper()
        select_binding = select_type_match.group(2)
    else:
        select_word = select_match.group(1)
        select_type = -1
        if select_word.lower().startswith("case"):
            select_type = 1
        elif select_word.lower().startswith("type"):
            select_type = 2
        select_binding = select_match.group(2)
    return "select", SELECT_info(select_type, select_binding, select_desc)


def read_type_def(line):
    """Attempt to read TYPE definition line"""
    type_match = TYPE_DEF_REGEX.match(line)
    if type_match is None:
        return None
    trailing_line = line[type_match.end(1) :].split("!")[0]
    trailing_line = trailing_line.strip()
    # Parse keywords
    keyword_match = TATTR_LIST_REGEX.match(trailing_line)
    keywords = []
    parent = None
    while keyword_match is not None:
        keyword_strip = keyword_match.group(0).replace(",", " ").strip().upper()
        extend_match = EXTENDS_REGEX.match(keyword_strip)
        if extend_match is not None:
            parent = extend_match.group(1).lower()
        else:
            keywords.append(keyword_strip)
        #
        trailing_line = trailing_line[keyword_match.end(0) :]
        keyword_match = TATTR_LIST_REGEX.match(trailing_line)
    # Get name
    line_split = trailing_line.split("::")
    if len(line_split) == 1:
        if len(keywords) > 0 and parent is None:
            return None
        else:
            if trailing_line.split("(")[0].strip().lower() == "is":
                return None
            trailing_line = line_split[0]
    else:
        trailing_line = line_split[1]
    #
    word_match = WORD_REGEX.match(trailing_line.strip())
    if word_match is not None:
        name = word_match.group(0)
    else:
        return None
    #
    return "typ", CLASS_info(name, parent, keywords)


def read_enum_def(line):
    """Attempt to read ENUM definition line"""
    enum_match = ENUM_DEF_REGEX.match(line)
    if enum_match is not None:
        return "enum", None
    return None


def read_generic_def(line):
    """Attempt to read generic procedure definition line"""
    generic_match = GENERIC_PRO_REGEX.match(line)
    if generic_match is None:
        return None
    #
    trailing_line = line[generic_match.end(0) - 1 :].split("!")[0].strip()
    if len(trailing_line) == 0:
        return None
    # Set visibility
    if generic_match.group(2) is None:
        vis_flag = 0
    else:
        if generic_match.group(2).lower() == "private":
            vis_flag = -1
        else:
            vis_flag = 1
    #
    i1 = trailing_line.find("=>")
    if i1 < 0:
        return None
    bound_name = trailing_line[:i1].strip()
    if GEN_ASSIGN_REGEX.match(bound_name):
        return None
    pro_list = trailing_line[i1 + 2 :].split(",")
    #
    pro_out = []
    for bound_pro in pro_list:
        if len(bound_pro.strip()) > 0:
            pro_out.append(bound_pro.strip())
    if len(pro_out) == 0:
        return None
    #
    return "gen", GEN_info(bound_name, pro_out, vis_flag)


def read_mod_def(line):
    """Attempt to read MODULE and MODULE PROCEDURE definition lines"""
    mod_match = MOD_REGEX.match(line)
    if mod_match is None:
        return None
    else:
        name = mod_match.group(1)
        if name.lower() == "procedure":
            trailing_line = line[mod_match.end(1) :]
            pro_names = []
            line_split = trailing_line.split(",")
            for name in line_split:
                pro_names.append(name.strip().lower())
            return "int_pro", pro_names
        # Check for submodule definition
        trailing_line = line[mod_match.start(1) :]
        sub_res = read_sub_def(trailing_line, mod_flag=True)
        if sub_res is not None:
            return sub_res
        fun_res = read_var_def(trailing_line, fun_only=True)
        if fun_res is not None:
            return fun_res[0], fun_res[1]._replace(mod_flag=True)
        fun_res = read_fun_def(trailing_line, mod_flag=True)
        if fun_res is not None:
            return fun_res
        return "mod", name


def read_submod_def(line):
    """Attempt to read SUBMODULE definition line"""
    submod_match = SUBMOD_REGEX.match(line)
    if submod_match is None:
        return None
    else:
        parent_name = None
        name = None
        trailing_line = line[submod_match.end(0) :].split("!")[0]
        trailing_line = trailing_line.strip()
        parent_match = WORD_REGEX.match(trailing_line)
        if parent_match is not None:
            parent_name = parent_match.group(0).lower()
            if len(trailing_line) > parent_match.end(0) + 1:
                trailing_line = trailing_line[parent_match.end(0) + 1 :].strip()
            else:
                trailing_line = ""
        #
        name_match = WORD_REGEX.search(trailing_line)
        if name_match is not None:
            name = name_match.group(0).lower()
        return "smod", SMOD_info(name, parent_name)


def read_prog_def(line):
    """Attempt to read PROGRAM definition line"""
    prog_match = PROG_REGEX.match(line)
    if prog_match is None:
        return None
    else:
        return "prog", prog_match.group(1)


def read_int_def(line):
    """Attempt to read INTERFACE definition line"""
    int_match = INT_REGEX.match(line)
    if int_match is None:
        return None
    else:
        int_name = int_match.group(2).lower()
        is_abstract = int_match.group(1) is not None
        if int_name == "":
            return "int", INT_info(None, is_abstract)
        if int_name == "assignment" or int_name == "operator":
            return "int", INT_info(None, False)
        return "int", INT_info(int_match.group(2), is_abstract)


def read_use_stmt(line):
    """Attempt to read USE statement"""
    use_match = USE_REGEX.match(line)
    if use_match is None:
        return None

    trailing_line = line[use_match.end(0) :].lower()
    use_mod = use_match.group(2)
    only_list = []
    rename_map = {}
    if use_match.group(3):
        for only_stmt in trailing_line.split(","):
            only_split = only_stmt.split("=>")
            only_name = only_split[0].strip()
            only_list.append(only_name)
            if len(only_split) == 2:
                rename_map[only_name] = only_split[1].strip()
    return "use", USE_info(use_mod, only_list, rename_map)


def read_imp_stmt(line):
    """Attempt to read IMPORT statement"""
    import_match = IMPORT_REGEX.match(line)
    if import_match is None:
        return None

    trailing_line = line[import_match.end(0) - 1 :].lower()
    import_list = [import_obj.strip() for import_obj in trailing_line.split(",")]
    return "import", import_list


def read_inc_stmt(line):
    """Attempt to read INCLUDE statement"""
    inc_match = INCLUDE_REGEX.match(line)
    if inc_match is None:
        return None
    else:
        inc_path = inc_match.group(1)
        return "inc", inc_path


def read_vis_stmnt(line):
    """Attempt to read PUBLIC/PRIVATE statement"""
    vis_match = VIS_REGEX.match(line)
    if vis_match is None:
        return None
    else:
        vis_type = 0
        if vis_match.group(1).lower() == "private":
            vis_type = 1
        trailing_line = line[vis_match.end(0) :].split("!")[0]
        mod_words = WORD_REGEX.findall(trailing_line)
        return "vis", VIS_info(vis_type, mod_words)


def_tests = [
    read_var_def,
    read_sub_def,
    read_fun_def,
    read_block_def,
    read_associate_def,
    read_select_def,
    read_type_def,
    read_enum_def,
    read_use_stmt,
    read_imp_stmt,
    read_int_def,
    read_generic_def,
    read_mod_def,
    read_prog_def,
    read_submod_def,
    read_inc_stmt,
    read_vis_stmnt,
]


class fortran_file:
    def __init__(self, path: str = None, pp_suffixes: list = None):
        self.path: str = path
        self.contents_split: list = []
        self.contents_pp: list = []
        self.pp_defs: dict = {}
        self.nLines: int = 0
        self.fixed: bool = False
        self.preproc: bool = False
        self.ast: fortran_ast = None
        self.hash: str = None
        if path is not None:
            _, file_ext = os.path.splitext(os.path.basename(path))
            if pp_suffixes is not None:
                self.preproc = file_ext in pp_suffixes
            else:
                self.preproc = file_ext == file_ext.upper()
        else:
            self.preproc = False

    def copy(self) -> fortran_file:
        """Copy content to new file object (does not copy objects)"""
        copy_obj = fortran_file(self.path)
        copy_obj.preproc = self.preproc
        copy_obj.fixed = self.fixed
        copy_obj.contents_pp = self.contents_pp
        copy_obj.contents_split = self.contents_split
        copy_obj.pp_defs = self.pp_defs
        copy_obj.set_contents(self.contents_split)
        return copy_obj

    def load_from_disk(self) -> tuple[str | None, bool | None]:
        """Read file from disk or update file contents only if they have changed
        A MD5 hash is used to determine that

        Returns
        -------
        tuple[str|None, bool|None]
            `str` : string containing IO error message else None
            `bool`: boolean indicating if the file has changed
        """
        contents: str
        try:
            if PY3K:
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    contents = re.sub(r"\t", r" ", f.read())
            else:
                with io.open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    contents = re.sub(r"\t", r" ", f.read())
        except OSError:
            return "Could not read/decode file", None
        else:
            # Check if files are the same
            hash = hashlib.md5(contents.encode("utf-8")).hexdigest()
            if hash == self.hash:
                return None, False

            self.hash = hash
            self.contents_split = contents.splitlines()
            self.fixed = detect_fixed_format(self.contents_split)
            self.contents_pp = self.contents_split
            self.nLines = len(self.contents_split)
            return None, True

    def apply_change(self, change: dict) -> bool:
        """Apply a change to the file."""

        def check_change_reparse(line_number: int) -> bool:
            if (line_number < 0) or (line_number > self.nLines - 1):
                return True
            pre_lines, curr_line, _ = self.get_code_line(line_number, forward=False)
            # Skip comment lines
            if self.fixed:
                if FIXED_COMMENT_LINE_MATCH.match(curr_line):
                    return False
            else:
                if FREE_COMMENT_LINE_MATCH.match(curr_line):
                    return False
            # Check for line labels and semicolons
            full_line = "".join(pre_lines) + curr_line
            full_line, line_label = strip_line_label(full_line)
            if line_label is not None:
                return True
            line_stripped = strip_strings(full_line, maintain_len=True)
            if line_stripped.find(";") >= 0:
                return True
            # Find trailing comments
            comm_ind = line_stripped.find("!")
            if comm_ind >= 0:
                line_no_comment = full_line[:comm_ind]
            else:
                line_no_comment = full_line
            # Various single line tests
            if END_WORD_REGEX.match(line_no_comment):
                return True
            if IMPLICIT_REGEX.match(line_no_comment):
                return True
            if CONTAINS_REGEX.match(line_no_comment):
                return True
            # Generic "non-definition" line
            if NON_DEF_REGEX.match(line_no_comment):
                return False
            # Loop through tests
            for test in def_tests:
                if test(line_no_comment):
                    return True
            return False

        self.hash = None
        text = change.get("text", "")
        change_range = change.get("range")
        if not PY3K:
            text = text.encode("utf-8")
        if len(text) == 0:
            text_split = [""]
        else:
            text_split = text.splitlines()
            # Check for ending newline
            if (text[-1] == "\n") or (text[-1] == "\r"):
                text_split.append("")

        if change_range is None:
            # The whole file has changed
            self.set_contents(text_split)
            return True

        start_line = change_range["start"]["line"]
        start_col = change_range["start"]["character"]
        end_line = change_range["end"]["line"]
        end_col = change_range["end"]["character"]

        # Check for an edit occuring at the very end of the file
        if start_line == self.nLines:
            self.set_contents(self.contents_split + text_split)
            return True

        # Check for single line edit
        if (start_line == end_line) and (len(text_split) == 1):
            prev_line = self.contents_split[start_line]
            self.contents_split[start_line] = (
                prev_line[:start_col] + text + prev_line[end_col:]
            )
            self.contents_pp[start_line] = self.contents_split[start_line]
            return check_change_reparse(start_line)

        # Apply standard change to document
        new_contents = []
        for i, line in enumerate(self.contents_split):
            if (i < start_line) or (i > end_line):
                new_contents.append(line)
                continue

            if i == start_line:
                for j, change_line in enumerate(text_split):
                    if j == 0:
                        new_contents.append(line[:start_col] + change_line)
                    else:
                        new_contents.append(change_line)

            if i == end_line:
                new_contents[-1] += line[end_col:]
        self.set_contents(new_contents)
        return True

    def set_contents(self, contents_split: list, detect_format: bool = True):
        """Set file contents"""
        self.contents_split = contents_split
        self.contents_pp = self.contents_split
        self.nLines = len(self.contents_split)
        if detect_format:
            self.fixed = detect_fixed_format(self.contents_split)

    def get_line(self, line_number: int, pp_content: bool = False) -> str:
        """Get single line from file"""
        try:
            if pp_content:
                return self.contents_pp[line_number]
            else:
                return self.contents_split[line_number]
        except:
            return None

    def get_code_line(
        self,
        line_number: int,
        forward: bool = True,
        backward: bool = True,
        pp_content: bool = False,
        strip_comment: bool = False,
    ) -> tuple[list[str], str, list[str]]:
        """Get full code line from file including any adjacent continuations"""
        curr_line = self.get_line(line_number, pp_content)
        if curr_line is None:
            return [], None, []
        # Search backward for prefix lines
        line_ind = line_number - 1
        pre_lines = []
        if backward:
            if self.fixed:  # Fixed format file
                tmp_line = curr_line
                while line_ind > 0:
                    if FIXED_CONT_REGEX.match(tmp_line):
                        prev_line = tmp_line
                        tmp_line = self.get_line(line_ind, pp_content)
                        if line_ind == line_number - 1:
                            curr_line = " " * 6 + curr_line[6:]
                        else:
                            pre_lines[-1] = " " * 6 + prev_line[6:]
                        pre_lines.append(tmp_line)
                    else:
                        break
                    line_ind -= 1
            else:  # Free format file
                opt_cont_match = FREE_CONT_REGEX.match(curr_line)
                if opt_cont_match is not None:
                    curr_line = (
                        " " * opt_cont_match.end(0) + curr_line[opt_cont_match.end(0) :]
                    )
                while line_ind > 0:
                    tmp_line = strip_strings(
                        self.get_line(line_ind, pp_content), maintain_len=True
                    )
                    tmp_no_comm = tmp_line.split("!")[0]
                    cont_ind = tmp_no_comm.rfind("&")
                    opt_cont_match = FREE_CONT_REGEX.match(tmp_no_comm)
                    if opt_cont_match is not None:
                        if cont_ind == opt_cont_match.end(0) - 1:
                            break
                        tmp_no_comm = (
                            " " * opt_cont_match.end(0)
                            + tmp_no_comm[opt_cont_match.end(0) :]
                        )
                    if cont_ind >= 0:
                        pre_lines.append(tmp_no_comm[:cont_ind])
                    else:
                        break
                    line_ind -= 1
        # Search forward for trailing lines with continuations
        line_ind = line_number + 1
        post_lines = []
        if forward:
            if self.fixed:
                if line_ind < self.nLines:
                    next_line = self.get_line(line_ind, pp_content)
                    line_ind += 1
                    cont_match = FIXED_CONT_REGEX.match(next_line)
                    while (cont_match is not None) and (line_ind < self.nLines):
                        post_lines.append(" " * 6 + next_line[6:])
                        next_line = self.get_line(line_ind, pp_content)
                        line_ind += 1
                        cont_match = FIXED_CONT_REGEX.match(next_line)
            else:
                line_stripped = strip_strings(curr_line, maintain_len=True)
                iAmper = line_stripped.find("&")
                iComm = line_stripped.find("!")
                if iComm < 0:
                    iComm = iAmper + 1
                next_line = ""
                while (iAmper >= 0) and (iAmper < iComm):
                    if line_ind == line_number + 1:
                        curr_line = curr_line[:iAmper]
                    elif next_line != "":
                        post_lines[-1] = next_line[:iAmper]
                    next_line = self.get_line(line_ind, pp_content)
                    line_ind += 1
                    # Skip empty or comment lines
                    match = FREE_COMMENT_LINE_MATCH.match(next_line)
                    if (next_line.rstrip() == "") or (match is not None):
                        next_line = ""
                        post_lines.append("")
                        continue
                    opt_cont_match = FREE_CONT_REGEX.match(next_line)
                    if opt_cont_match is not None:
                        next_line = (
                            " " * opt_cont_match.end(0)
                            + next_line[opt_cont_match.end(0) :]
                        )
                    post_lines.append(next_line)
                    line_stripped = strip_strings(next_line, maintain_len=True)
                    iAmper = line_stripped.find("&")
                    iComm = line_stripped.find("!")
                    if iComm < 0:
                        iComm = iAmper + 1
        # Detect start of comment in current line
        if strip_comment:
            curr_line = self.strip_comment(curr_line)
        pre_lines.reverse()
        return pre_lines, curr_line, post_lines

    def strip_comment(self, line: str) -> str:
        """Strip comment from line"""
        if self.fixed:
            if (FIXED_COMMENT_LINE_MATCH.match(line) is not None) and (
                FIXED_OPENMP_MATCH.match(line) is not None
            ):
                return ""
        else:
            if FREE_OPENMP_MATCH.match(line) is None:
                line = line.split("!")[0]
        return line

    def find_word_in_code_line(
        self,
        line_number: int,
        word: str,
        forward: bool = True,
        backward: bool = False,
        pp_content: bool = False,
    ) -> tuple[int, int, int]:
        back_lines, curr_line, forward_lines = self.get_code_line(
            line_number, forward=forward, backward=backward, pp_content=pp_content
        )
        i0 = i1 = -1
        if curr_line is not None:
            find_word_lower = word.lower()
            i0, i1 = find_word_in_line(curr_line.lower(), find_word_lower)
        if backward and (i0 < 0):
            back_lines.reverse()
            for (i, line) in enumerate(back_lines):
                i0, i1 = find_word_in_line(line.lower(), find_word_lower)
                if i0 >= 0:
                    line_number -= i + 1
                    return line_number, i0, i1
        if forward and (i0 < 0):
            for (i, line) in enumerate(forward_lines):
                i0, i1 = find_word_in_line(line.lower(), find_word_lower)
                if i0 >= 0:
                    line_number += i + 1
                    return line_number, i0, i1
        return line_number, i0, i1

    def preprocess(
        self, pp_defs: dict = {}, include_dirs: list = [], debug: bool = False
    ) -> tuple[list, list]:
        self.contents_pp, pp_skips, pp_defines, self.pp_defs = preprocess_file(
            self.contents_split,
            self.path,
            pp_defs=pp_defs,
            include_dirs=include_dirs,
            debug=debug,
        )
        return pp_skips, pp_defines

    def check_file(self, obj_tree, max_line_length=-1, max_comment_line_length=-1):
        diagnostics = []
        if (max_line_length > 0) or (max_comment_line_length > 0):
            line_message = 'Line length exceeds "max_line_length" ({0})'.format(
                max_line_length
            )
            comment_message = (
                'Comment line length exceeds "max_comment_line_length" ({0})'.format(
                    max_comment_line_length
                )
            )
            if self.fixed:
                COMMENT_LINE_MATCH = FIXED_COMMENT_LINE_MATCH
            else:
                COMMENT_LINE_MATCH = FREE_COMMENT_LINE_MATCH
            for (i, line) in enumerate(self.contents_split):
                if COMMENT_LINE_MATCH.match(line) is None:
                    if (max_line_length > 0) and (len(line) > max_line_length):
                        diagnostics.append(
                            {
                                "range": {
                                    "start": {"line": i, "character": max_line_length},
                                    "end": {"line": i, "character": len(line)},
                                },
                                "message": line_message,
                                "severity": 2,
                            }
                        )
                else:
                    if (max_comment_line_length > 0) and (
                        len(line) > max_comment_line_length
                    ):
                        diagnostics.append(
                            {
                                "range": {
                                    "start": {
                                        "line": i,
                                        "character": max_comment_line_length,
                                    },
                                    "end": {"line": i, "character": len(line)},
                                },
                                "message": comment_message,
                                "severity": 2,
                            }
                        )
        errors, diags_ast = self.ast.check_file(obj_tree)
        diagnostics += diags_ast
        for error in errors:
            diagnostics.append(error.build(self))
        return diagnostics


def preprocess_file(
    contents_split: list,
    file_path: str = None,
    pp_defs: dict = {},
    include_dirs: list = [],
    debug: bool = False,
):
    # Look for and mark excluded preprocessor paths in file
    # Initial implementation only looks for "if" and "ifndef" statements.
    # For "if" statements all blocks are excluded except the "else" block if present
    # For "ifndef" statements all blocks excluding the first block are excluded
    def eval_pp_if(text, defs: dict = {}):
        def replace_ops(expr: str):
            expr = expr.replace("&&", " and ")
            expr = expr.replace("||", " or ")
            expr = expr.replace("!=", " <> ")
            expr = expr.replace("!", " not ")
            expr = expr.replace(" <> ", " != ")
            return expr

        def replace_defined(line: str):
            i0 = 0
            out_line = ""
            for match in DEFINED_REGEX.finditer(line):
                if match.group(1) in defs:
                    out_line += line[i0 : match.start(0)] + "($@)"
                else:
                    out_line += line[i0 : match.start(0)] + "($%)"
                i0 = match.end(0)
            if i0 < len(line):
                out_line += line[i0:]
            return out_line

        def replace_vars(line: str):
            i0 = 0
            out_line = ""
            for match in WORD_REGEX.finditer(line):
                if match.group(0) in defs:
                    out_line += line[i0 : match.start(0)] + defs[match.group(0)]
                else:
                    out_line += line[i0 : match.start(0)] + "False"
                i0 = match.end(0)
            if i0 < len(line):
                out_line += line[i0:]
            out_line = out_line.replace("$@", "True")
            out_line = out_line.replace("$%", "False")
            return out_line

        out_line = replace_defined(text)
        out_line = replace_vars(out_line)
        try:
            line_res = eval(replace_ops(out_line))
        except:
            return False
        else:
            return line_res

    #
    if file_path is not None:
        include_dirs = [os.path.dirname(file_path)] + include_dirs
    pp_skips = []
    pp_defines = []
    pp_stack = []
    defs_tmp = pp_defs.copy()
    def_regexes = {}
    output_file = []
    def_cont_name = None
    for (i, line) in enumerate(contents_split):
        # Handle multiline macro continuation
        if def_cont_name is not None:
            output_file.append("")
            if line.rstrip()[-1] != "\\":
                defs_tmp[def_cont_name] += line.strip()
                def_cont_name = None
            else:
                defs_tmp[def_cont_name] += line[0:-1].strip()
            continue
        # Handle conditional statements
        match = PP_REGEX.match(line)
        if match:
            output_file.append(line)
            def_name = None
            if_start = False
            # Opening conditional statements
            if match.group(1) == "if ":
                is_path = eval_pp_if(line[match.end(1) :], defs_tmp)
                if_start = True
            elif match.group(1) == "ifdef":
                if_start = True
                def_name = line[match.end(0) :].strip()
                is_path = def_name in defs_tmp
            elif match.group(1) == "ifndef":
                if_start = True
                def_name = line[match.end(0) :].strip()
                is_path = not (def_name in defs_tmp)
            if if_start:
                if is_path:
                    pp_stack.append([-1, -1])
                    log.debug(f"{line.strip()} !!! Conditional TRUE({i+1})")
                else:
                    pp_stack.append([i + 1, -1])
                    log.debug(f"{line.strip()} !!! Conditional FALSE({i+1})")
                continue
            if len(pp_stack) == 0:
                continue
            # Closing/middle conditional statements
            inc_start = False
            exc_start = False
            if match.group(1) == "elif":
                if pp_stack[-1][0] < 0:
                    pp_stack[-1][0] = i + 1
                    exc_start = True
                else:
                    if eval_pp_if(line[match.end(1) :], defs_tmp):
                        pp_stack[-1][1] = i - 1
                        pp_stack.append([-1, -1])
                        inc_start = True
            elif match.group(1) == "else":
                if pp_stack[-1][0] < 0:
                    pp_stack[-1][0] = i + 1
                    exc_start = True
                else:
                    pp_stack[-1][1] = i + 1
                    inc_start = True
            elif match.group(1) == "endif":
                if pp_stack[-1][0] < 0:
                    pp_stack.pop()
                    continue
                if pp_stack[-1][1] < 0:
                    pp_stack[-1][1] = i + 1
                    log.debug(f"{line.strip()} !!! Conditional FALSE/END({i+1})")
                pp_skips.append(pp_stack.pop())
            if debug:
                if inc_start:
                    log.debug(f"{line.strip()} !!! Conditional TRUE({i+1})")
                elif exc_start:
                    log.debug(f"{line.strip()} !!! Conditional FALSE({i+1})")
            continue
        # Handle variable/macro definitions files
        match = PP_DEF_REGEX.match(line)
        if (match is not None) and ((len(pp_stack) == 0) or (pp_stack[-1][0] < 0)):
            output_file.append(line)
            pp_defines.append(i + 1)
            def_name = match.group(2)
            if (match.group(1) == "define") and (def_name not in defs_tmp):
                eq_ind = line[match.end(0) :].find(" ")
                if eq_ind >= 0:
                    # Handle multiline macros
                    if line.rstrip()[-1] == "\\":
                        defs_tmp[def_name] = line[match.end(0) + eq_ind : -1].strip()
                        def_cont_name = def_name
                    else:
                        defs_tmp[def_name] = line[match.end(0) + eq_ind :].strip()
                else:
                    defs_tmp[def_name] = "True"
            elif (match.group(1) == "undef") and (def_name in defs_tmp):
                defs_tmp.pop(def_name, None)
            log.debug(f"{line.strip()} !!! Define statement({i+1})")
            continue
        # Handle include files
        match = PP_INCLUDE_REGEX.match(line)
        if (match is not None) and ((len(pp_stack) == 0) or (pp_stack[-1][0] < 0)):
            log.debug(f"{line.strip()} !!! Include statement({i+1})")
            include_filename = match.group(1).replace('"', "")
            include_path = None
            # Intentionally keep this as a list and not a set. There are cases
            # where projects play tricks with the include order of their headers
            # to get their codes to compile. Using a set would not permit that.
            for include_dir in include_dirs:
                include_path_tmp = os.path.join(include_dir, include_filename)
                if os.path.isfile(include_path_tmp):
                    include_path = os.path.abspath(include_path_tmp)
                    break
            if include_path is not None:
                try:
                    include_file = fortran_file(include_path)
                    err_string, _ = include_file.load_from_disk()
                    if err_string is None:
                        log.debug(f'\n!!! Parsing include file "{include_path}"')
                        _, _, _, defs_tmp = preprocess_file(
                            include_file.contents_split,
                            file_path=include_path,
                            pp_defs=defs_tmp,
                            include_dirs=include_dirs,
                            debug=debug,
                        )
                        log.debug("!!! Completed parsing include file\n")

                    else:
                        log.debug(f"!!! Failed to parse include file: {err_string}")

                except:
                    log.debug("!!! Failed to parse include file: exception")

            else:
                log.debug(f"{line.strip()} !!! Could not locate include file ({i+1})")

        # Substitute (if any) read in preprocessor macros
        for def_tmp, value in defs_tmp.items():
            def_regex = def_regexes.get(def_tmp)
            if def_regex is None:
                def_regex = re.compile(r"\b{0}\b".format(def_tmp))
                def_regexes[def_tmp] = def_regex
            line_new, nsubs = def_regex.subn(value, line)
            if nsubs > 0:
                log.debug(f"{line.strip()} !!! Macro sub({i+1}) '{def_tmp}' -> {value}")
                line = line_new
        output_file.append(line)
    return output_file, pp_skips, pp_defines, defs_tmp


def process_file(
    file_obj: fortran_file,
    debug: bool = False,
    pp_defs: dict = {},
    include_dirs: list = [],
):
    """Build file AST by parsing file"""

    def parser_debug_msg(msg: str, line: str, ln: int):
        log.debug(f"{line.strip()} !!! {msg} statement({ln})")

    # Configure the parser logger
    if debug:
        logging.basicConfig(
            level=logging.DEBUG, stream=sys.stdout, format="%(message)s"
        )

    file_ast = fortran_ast(file_obj)
    if file_obj.preproc:
        log.debug("=== PreProc Pass ===\n")
        pp_skips, pp_defines = file_obj.preprocess(
            pp_defs=pp_defs, include_dirs=include_dirs, debug=debug
        )
        for pp_reg in pp_skips:
            file_ast.start_ppif(pp_reg[0])
            file_ast.end_ppif(pp_reg[1])
        log.debug("\n=== Parsing Pass ===\n")
    else:
        log.debug("=== No PreProc ===\n")
        pp_skips = []
        pp_defines = []
    #
    line_ind = 0
    next_line_ind = 0
    line_number = 1
    int_counter = 0
    block_counter = 0
    do_counter = 0
    if_counter = 0
    select_counter = 0
    block_id_stack = []
    semi_split = []
    doc_string: str = None
    if file_obj.fixed:
        COMMENT_LINE_MATCH = FIXED_COMMENT_LINE_MATCH
        DOC_COMMENT_MATCH = FIXED_DOC_MATCH
    else:
        COMMENT_LINE_MATCH = FREE_COMMENT_LINE_MATCH
        DOC_COMMENT_MATCH = FREE_DOC_MATCH
    while (next_line_ind < file_obj.nLines) or (len(semi_split) > 0):
        # Get next line
        if len(semi_split) > 0:
            line = semi_split[0]
            semi_split = semi_split[1:]
            get_full = False
        else:
            line_ind = next_line_ind
            line_number = line_ind + 1
            line = file_obj.get_line(line_ind, pp_content=True)
            next_line_ind = line_ind + 1
            get_full = True
        if line == "":
            continue  # Skip empty lines
        # Skip comment lines
        match = COMMENT_LINE_MATCH.match(line)
        if match:
            # Check for documentation
            doc_match = DOC_COMMENT_MATCH.match(line)
            if doc_match:
                doc_lines = [line[doc_match.end(0) :].strip()]
                if doc_match.group(1) == ">":
                    doc_forward = True
                else:
                    if doc_string:
                        doc_lines = [doc_string] + doc_lines
                        doc_string = None
                    doc_forward = False
                if next_line_ind < file_obj.nLines:
                    next_line = file_obj.get_line(next_line_ind, pp_content=True)
                    next_line_ind += 1
                    doc_match = DOC_COMMENT_MATCH.match(next_line)
                    while (doc_match is not None) and (next_line_ind < file_obj.nLines):
                        doc_lines.append(next_line[doc_match.end(0) :].strip())
                        next_line = file_obj.get_line(next_line_ind, pp_content=True)
                        next_line_ind += 1
                        doc_match = DOC_COMMENT_MATCH.match(next_line)
                    next_line_ind -= 1
                if debug:
                    for (i, doc_line) in enumerate(doc_lines):
                        log.debug(f"{doc_line} !!! Doc string({line_number+i})")
                line_sum = 0
                for doc_line in doc_lines:
                    line_sum += len(doc_line)
                if line_sum > 0:
                    file_ast.add_doc(
                        "!! " + "\n!! ".join(doc_lines), forward=doc_forward
                    )
            continue
        # Handle trailing doc strings
        if doc_string:
            file_ast.add_doc("!! " + doc_string)
            log.debug(f"{doc_string} !!! Doc string({line_number})")
            doc_string = None
        # Handle preprocessing regions
        do_skip = False
        for pp_reg in pp_skips:
            if (line_number >= pp_reg[0]) and (line_number <= pp_reg[1]):
                do_skip = True
                break
        if line_number in pp_defines:
            do_skip = True
        if do_skip:
            continue
        # Get full line
        if get_full:
            _, line, post_lines = file_obj.get_code_line(
                line_ind, backward=False, pp_content=True
            )
            next_line_ind += len(post_lines)
            line = "".join([line] + post_lines)
        # print(line)
        line, line_label = strip_line_label(line)
        line_stripped = strip_strings(line, maintain_len=True)
        # Find trailing comments
        comm_ind = line_stripped.find("!")
        if comm_ind >= 0:
            line_no_comment = line[:comm_ind]
            line_post_comment = line[comm_ind:]
            line_stripped = line_stripped[:comm_ind]
        else:
            line_no_comment = line
            line_post_comment = None
        # Split lines with semicolons
        semi_colon_ind = line_stripped.find(";")
        if semi_colon_ind > 0:
            semi_inds = []
            tmp_line = line_stripped
            while semi_colon_ind >= 0:
                semi_inds.append(semi_colon_ind)
                tmp_line = tmp_line[semi_colon_ind + 1 :]
                semi_colon_ind = tmp_line.find(";")
            i0 = 0
            for semi_colon_ind in semi_inds:
                semi_split.append(line[i0 : i0 + semi_colon_ind])
                i0 += semi_colon_ind + 1
            if len(semi_split) > 0:
                semi_split.append(line[i0:])
                line = semi_split[0]
                semi_split = semi_split[1:]
                line_stripped = strip_strings(line, maintain_len=True)
                line_no_comment = line
                line_post_comment = None
        # Test for scope end
        if file_ast.END_SCOPE_REGEX is not None:
            match = END_WORD_REGEX.match(line_no_comment)
            # Handle end statement
            if match:
                end_scope_word = None
                if match.group(1) is None:
                    end_scope_word = ""
                    if file_ast.current_scope.req_named_end() and (
                        file_ast.current_scope is not file_ast.none_scope
                    ):
                        file_ast.end_errors.append(
                            [line_number, file_ast.current_scope.sline]
                        )
                else:
                    scope_match = file_ast.END_SCOPE_REGEX.match(
                        line_no_comment[match.start(1) :]
                    )
                    if scope_match is not None:
                        end_scope_word = scope_match.group(0)
                if end_scope_word is not None:
                    if (file_ast.current_scope.get_type() == SELECT_TYPE_ID) and (
                        file_ast.current_scope.is_type_region()
                    ):
                        file_ast.end_scope(line_number)
                    file_ast.end_scope(line_number)
                    log.debug(
                        f'{line.strip()} !!! END "{end_scope_word}"'
                        f" scope({line_number})"
                    )
                    continue
            # Look for old-style end of DO loops with line labels
            if (file_ast.current_scope.get_type() == DO_TYPE_ID) and (
                line_label is not None
            ):
                did_close = False
                while (len(block_id_stack) > 0) and (line_label == block_id_stack[-1]):
                    file_ast.end_scope(line_number)
                    block_id_stack.pop()
                    did_close = True
                    log.debug(f'{line.strip()} !!! END "DO" scope({line_number})')
                if did_close:
                    continue
        # Skip if known generic code line
        match = NON_DEF_REGEX.match(line_no_comment)
        if match:
            continue
        # Mark implicit statement
        match = IMPLICIT_REGEX.match(line_no_comment)
        if match:
            err_message = None
            if file_ast.current_scope is None:
                err_message = "IMPLICIT statement without enclosing scope"
            else:
                if match.group(1).lower() == "none":
                    file_ast.current_scope.set_implicit(False, line_number)
                else:
                    file_ast.current_scope.set_implicit(True, line_number)
            if err_message:
                file_ast.parse_errors.append(
                    {
                        "line": line_number,
                        "schar": match.start(1),
                        "echar": match.end(1),
                        "mess": err_message,
                        "sev": 1,
                    }
                )
            parser_debug_msg("IMPLICIT", line, line_number)
            continue
        # Mark contains statement
        match = CONTAINS_REGEX.match(line_no_comment)
        if match:
            err_message = None
            try:
                if file_ast.current_scope is None:
                    err_message = "CONTAINS statement without enclosing scope"
                else:
                    file_ast.current_scope.mark_contains(line_number)
            except ValueError:
                err_message = "Multiple CONTAINS statements in scope"
            if err_message:
                file_ast.parse_errors.append(
                    {
                        "line": line_number,
                        "schar": match.start(1),
                        "echar": match.end(1),
                        "mess": err_message,
                        "sev": 1,
                    }
                )
            parser_debug_msg("CONTAINS", line, line_number)
            continue
        # Look for trailing doc string
        if line_post_comment:
            doc_match = FREE_DOC_MATCH.match(line_post_comment)
            if doc_match:
                doc_string = line_post_comment[doc_match.end(0) :].strip()
        # Loop through tests
        obj_read = None
        for test in def_tests:
            obj_read = test(line_no_comment)
            if obj_read is not None:
                break
        #
        if obj_read is not None:
            obj_type = obj_read[0]
            obj_info = obj_read[1]
            if obj_type == "var":
                if obj_info.var_names is None:
                    continue
                desc_string = obj_info.type_word
                link_name = None
                procedure_def = False
                if desc_string[:3] == "PRO":
                    if file_ast.current_scope.get_type() == INTERFACE_TYPE_ID:
                        for var_name in obj_info.var_names:
                            file_ast.add_int_member(var_name)
                        parser_debug_msg("INTERFACE-PRO", line, line_number)
                        continue
                    procedure_def = True
                    link_name = get_paren_substring(desc_string)
                for var_name in obj_info.var_names:
                    link_name = None
                    if var_name.find("=>") > -1:
                        name_split = var_name.split("=>")
                        name_raw = name_split[0]
                        link_name = name_split[1].split("(")[0].strip()
                        if link_name.lower() == "null":
                            link_name = None
                    else:
                        name_raw = var_name.split("=")[0]
                    # Add dimension if specified
                    key_tmp = obj_info.keywords[:]
                    iparen = name_raw.find("(")
                    if iparen == 0:
                        continue
                    elif iparen > 0:
                        if name_raw[iparen - 1] == "*":
                            iparen -= 1
                            if desc_string.find("(") < 0:
                                desc_string += f"*({get_paren_substring(name_raw)})"
                        else:
                            key_tmp.append(
                                f"dimension({get_paren_substring(name_raw)})"
                            )
                        name_raw = name_raw[:iparen]
                    name_stripped = name_raw.strip()
                    keywords, keyword_info = map_keywords(key_tmp)
                    if procedure_def:
                        new_var = fortran_meth(
                            file_ast,
                            line_number,
                            name_stripped,
                            desc_string,
                            keywords,
                            keyword_info=keyword_info,
                            link_obj=link_name,
                        )
                    else:
                        new_var = fortran_var(
                            file_ast,
                            line_number,
                            name_stripped,
                            desc_string,
                            keywords,
                            keyword_info=keyword_info,
                            link_obj=link_name,
                        )
                        # If the object is fortran_var and a parameter include
                        #  the value in hover
                        if new_var.is_parameter():
                            _, col = find_word_in_line(line, name_stripped)
                            match = PARAMETER_VAL_REGEX.match(line[col:])
                            if match:
                                var = match.group(1).strip()
                                new_var.set_parameter_val(var)
                    file_ast.add_variable(new_var)
                parser_debug_msg("VARIABLE", line, line_number)

            elif obj_type == "mod":
                new_mod = fortran_module(file_ast, line_number, obj_info)
                file_ast.add_scope(new_mod, END_MOD_REGEX)
                parser_debug_msg("MODULE", line, line_number)

            elif obj_type == "smod":
                new_smod = fortran_submodule(
                    file_ast, line_number, obj_info.name, ancestor_name=obj_info.parent
                )
                file_ast.add_scope(new_smod, END_SMOD_REGEX)
                parser_debug_msg("SUBMODULE", line, line_number)

            elif obj_type == "prog":
                new_prog = fortran_program(file_ast, line_number, obj_info)
                file_ast.add_scope(new_prog, END_PROG_REGEX)
                parser_debug_msg("PROGRAM", line, line_number)

            elif obj_type == "sub":
                keywords, _ = map_keywords(obj_info.keywords)
                new_sub = fortran_subroutine(
                    file_ast,
                    line_number,
                    obj_info.name,
                    args=obj_info.args,
                    mod_flag=obj_info.mod_flag,
                    keywords=keywords,
                )
                file_ast.add_scope(new_sub, END_SUB_REGEX)
                parser_debug_msg("SUBROUTINE", line, line_number)

            elif obj_type == "fun":
                keywords, _ = map_keywords(obj_info.keywords)
                new_fun = fortran_function(
                    file_ast,
                    line_number,
                    obj_info.name,
                    args=obj_info.args,
                    mod_flag=obj_info.mod_flag,
                    keywords=keywords,
                    return_type=obj_info.return_type,
                    result_var=obj_info.return_var,
                )
                file_ast.add_scope(new_fun, END_FUN_REGEX)
                if obj_info.return_type is not None:
                    keywords, keyword_info = map_keywords(obj_info.return_type[1])
                    new_obj = fortran_var(
                        file_ast,
                        line_number,
                        obj_info.name,
                        obj_info.return_type[0],
                        keywords,
                        keyword_info,
                    )
                    file_ast.add_variable(new_obj)
                parser_debug_msg("FUNCTION", line, line_number)

            elif obj_type == "block":
                name = obj_info
                if name is None:
                    block_counter += 1
                    name = f"#BLOCK{block_counter}"
                new_block = fortran_block(file_ast, line_number, name)
                file_ast.add_scope(new_block, END_BLOCK_REGEX, req_container=True)
                parser_debug_msg("BLOCK", line, line_number)

            elif obj_type == "do":
                do_counter += 1
                name = f"#DO{do_counter}"
                if obj_info != "":
                    block_id_stack.append(obj_info)
                new_do = fortran_do(file_ast, line_number, name)
                file_ast.add_scope(new_do, END_DO_REGEX, req_container=True)
                parser_debug_msg("DO", line, line_number)

            elif obj_type == "where":
                # Add block if WHERE is not single line
                if not obj_info:
                    do_counter += 1
                    name = f"#WHERE{do_counter}"
                    new_do = fortran_where(file_ast, line_number, name)
                    file_ast.add_scope(new_do, END_WHERE_REGEX, req_container=True)
                parser_debug_msg("WHERE", line, line_number)

            elif obj_type == "assoc":
                block_counter += 1
                name = f"#ASSOC{block_counter}"
                new_assoc = fortran_associate(file_ast, line_number, name)
                file_ast.add_scope(new_assoc, END_ASSOCIATE_REGEX, req_container=True)
                for bound_var in obj_info:
                    binding_split = bound_var.split("=>")
                    if len(binding_split) == 2:
                        binding_name = binding_split[0].strip()
                        link_name = binding_split[1].strip()
                        file_ast.add_variable(
                            new_assoc.create_binding_variable(
                                file_ast, line_number, binding_name, link_name
                            )
                        )
                parser_debug_msg("ASSOCIATE", line, line_number)

            elif obj_type == "if":
                if_counter += 1
                name = f"#IF{if_counter}"
                new_if = fortran_if(file_ast, line_number, name)
                file_ast.add_scope(new_if, END_IF_REGEX, req_container=True)
                parser_debug_msg("IF", line, line_number)

            elif obj_type == "select":
                select_counter += 1
                name = f"#SELECT{select_counter}"
                new_select = fortran_select(file_ast, line_number, name, obj_info)
                file_ast.add_scope(new_select, END_SELECT_REGEX, req_container=True)
                new_var = new_select.create_binding_variable(
                    file_ast,
                    line_number,
                    f"{obj_info.desc}({obj_info.binding})",
                    obj_info.type,
                )
                if new_var is not None:
                    file_ast.add_variable(new_var)
                parser_debug_msg("SELECT", line, line_number)

            elif obj_type == "typ":
                keywords, _ = map_keywords(obj_info.keywords)
                new_type = fortran_type(file_ast, line_number, obj_info.name, keywords)
                if obj_info.parent is not None:
                    new_type.set_inherit(obj_info.parent)
                file_ast.add_scope(new_type, END_TYPED_REGEX, req_container=True)
                parser_debug_msg("TYPE", line, line_number)

            elif obj_type == "enum":
                block_counter += 1
                name = f"#ENUM{block_counter}"
                new_enum = fortran_enum(file_ast, line_number, name)
                file_ast.add_scope(new_enum, END_ENUMD_REGEX, req_container=True)
                parser_debug_msg("ENUM", line, line_number)

            elif obj_type == "int":
                name = obj_info.name
                if name is None:
                    int_counter += 1
                    name = f"#GEN_INT{int_counter}"
                new_int = fortran_int(
                    file_ast, line_number, name, abstract=obj_info.abstract
                )
                file_ast.add_scope(new_int, END_INT_REGEX, req_container=True)
                parser_debug_msg("INTERFACE", line, line_number)

            elif obj_type == "gen":
                new_int = fortran_int(
                    file_ast, line_number, obj_info.bound_name, abstract=False
                )
                new_int.set_visibility(obj_info.vis_flag)
                file_ast.add_scope(new_int, END_INT_REGEX, req_container=True)
                for pro_link in obj_info.pro_links:
                    file_ast.add_int_member(pro_link)
                file_ast.end_scope(line_number)
                parser_debug_msg("GENERIC", line, line_number)

            elif obj_type == "int_pro":
                if file_ast.current_scope is not None:
                    if file_ast.current_scope.get_type() == INTERFACE_TYPE_ID:
                        for name in obj_info:
                            file_ast.add_int_member(name)
                        parser_debug_msg("INTERFACE-PRO", line, line_number)

                    elif file_ast.current_scope.get_type() == SUBMODULE_TYPE_ID:
                        new_impl = fortran_scope(file_ast, line_number, obj_info[0])
                        file_ast.add_scope(new_impl, END_PRO_REGEX)
                        parser_debug_msg("INTERFACE_IMPL", line, line_number)

            elif obj_type == "use":
                file_ast.add_use(
                    obj_info.mod_name,
                    line_number,
                    obj_info.only_list,
                    obj_info.rename_map,
                )
                parser_debug_msg("USE", line, line_number)

            elif obj_type == "import":
                file_ast.add_use("#IMPORT", line_number, obj_info)
                parser_debug_msg("IMPORT", line, line_number)

            elif obj_type == "inc":
                file_ast.add_include(obj_info, line_number)
                parser_debug_msg("INCLUDE", line, line_number)

            elif obj_type == "vis":
                if file_ast.current_scope is None:
                    file_ast.parse_errors.append(
                        {
                            "line": line_number,
                            "schar": 0,
                            "echar": 0,
                            "mess": "Visibility statement without enclosing scope",
                            "sev": 1,
                        }
                    )
                else:
                    if (len(obj_info.obj_names) == 0) and (obj_info.type == 1):
                        file_ast.current_scope.set_default_vis(-1)
                    else:
                        if obj_info.type == 1:
                            for word in obj_info.obj_names:
                                file_ast.add_private(word)
                        else:
                            for word in obj_info.obj_names:
                                file_ast.add_public(word)
                parser_debug_msg("Visibility", line, line_number)

    file_ast.close_file(line_number)
    if debug:
        if len(file_ast.end_errors) > 0:
            log.debug("\n=== Scope Errors ===\n")
            for error in file_ast.end_errors:
                if error[0] >= 0:
                    message = f"Unexpected end of scope at line {error[0]}"
                else:
                    message = "Unexpected end statement: No open scopes"
                log.debug(f"{error[1]}: {message}")
        if len(file_ast.parse_errors) > 0:
            log.debug("\n=== Parsing Errors ===\n")
            for error in file_ast.parse_errors:
                log.debug(f"{error['line']}: {error['mess']}")
    return file_ast
