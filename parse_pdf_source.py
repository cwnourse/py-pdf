# -*- coding: utf-8 -*-
"""
parses PDF as text file.
uncompress pdf with pdftk first: pdftk in.pdf output out.pdf uncompress. 
otherwise will not be plain text

parser implemented based on ISO 32000-2:2020(E) (PDF2.0)

@author: noursec
"""
import time
# from collections import namedtuple

# Token = namedtuple('Token', ['type','value','pos'])
class Token:
    def __init__(self,token_type,value,pos):
        self.type = token_type
        self.value = value
        self.pos = pos
    def __repr__(self):
        return f'Token<{self.type}><{self.value}><{self.pos}>'
    def __str__(self):
        return f'Token<{self.type}>'
    def __eq__(self, o):
        return self.type == o.type
    def __contains__(self,o):
        return self.value.__contains__(o)
    
        
def readBytes(file, chunk=16384):
    # generator to iterate over raw bytes of input file
    # experiment with different methods for speedup eg just copy whole pdf into RAM at once
    with open(file,'rb') as f:
        data = f.read()
        return data
        # while fchunk := f.read(chunk):
        #     yield from fchunk

class PdfInterpreter():
    # char classes.
    CHAR_WS = [0,9,10,12,13,32]                           + [b'\x00',b'\t',b'\n',b'\x0c',b'\r',b' ']                     
    CHAR_EOL = [10,13]                                    + [b'\n',b'\r']
    CHAR_DELIM = [40,41,60,62,91,93,123,125,47,37]        + [b'(',b')',b'<',b'>',b'[',b']',b'{',b'}',b'/',b'%']                           
    CHAR_NUM = [48,49,50,51,52,53,54,55,56,57,43,45,46]   + [b'0',b'1',b'2',b'3',b'4',b'5',b'6',b'7',b'8',b'9',b'+',b'-',b'.']  
    CHAR_NONREG = CHAR_WS + CHAR_DELIM 
    KEYWORDS = ['obj','endobj',b'stream',b'endstream','R','true','false','xref','f','n','trailer','startxref']
    
    def __init__(self,filename):
        self.data = readBytes(filename)
        self.reader = iter(self.data)
        # self.reader = readBytes(filename)
        
        self.tokens = []   # stack of tokens
        self.bytes = []   # stack of read bytes
        self.pos = -1      # current byte offset into file such that file[pos]=bytes[-1]. 0=first byte
        self.line = 1      # current line number, delim by /n, /r, or /r/n
        self.peek = 0      # current 'look ahead' in file. nextByte returns from byte stack
        self.xrefLoc = None
        self.EOF = False
        
    def flushStack(self):
        # flushes stack, returns buffered bytes. 
        # does not return 'peeked' bytes
        stack=self.bytes[:len(self.bytes)-self.peek]
        self.bytes = self.bytes[len(self.bytes)-self.peek:]
        return stack
    
    def pushToken(self,token:Token):
        self.tokens.append(token)

    # @profile
    def popByte(self,n=1):
        # remove and return last tokens added to stack self.byte, oldest first order
        pop,self.bytes = self.bytes[-1*n:],self.bytes[:-1*n]
        self.peek -= n if self.peek else 0               # make sure to decrement peek if present
        if self.peek<0: self.peek=0
        return pop  # raw int value of byte
    
      
    ##############################################
    # tokenizer, call nextToken() while true to generate all tokens. perhaps make this a generator
    
    def tokenize(self):
        if not self.EOF:
            while self.nextToken(): continue
        return None
        
    def newToken(self,token_type,value,position) -> Token:
        t = Token(token_type,value,position)
        self.tokens.append(t)
        return t
    
    # @profile
    def nextToken(self):
        # parse non-recurive PDF syntax tokens (ie. will make a token for a comment, but the parser will have to handle nested dictionaries etc)
        # also will not handle 'look back' tokens. ie to make an object token we have to see 'obj' or 'R' and then pop the last two int tokens to make object token.
        # backtracking behavior would not make sense in the context of nextToken as a generator, since we'd see int,ws,int,ws,OBJ=[int,int,'obj|'r'],
            # and it would not be efficient to look forward every time we see an int. we'll make the object in the parser
        
        # PDF binaries are typically computer generated. so we will not do rigorous syntax checking,
        # assume the input doc conforms to PDF spec (eg. comments shall be terminated by newline)
  
        if self.EOF:
            return False
        
        b = self.nextByte()
        
        pos=self.pos-self.peek       # start position (byte offset) of this token
        token_type = 'NONE'
        
        if b in self.CHAR_WS:  # consume all whitespace chars including spaces and newline as one (7.2.3)
            token_type = 'CHAR_EOL' if b in self.CHAR_EOL else 'CHAR_WS'
            while (b:=self.nextByte()) in self.CHAR_WS:
                if b in self.CHAR_EOL: token_type = 'CHAR_EOL'  # no diff between ' \n', '     \n', '\n'. all EOL tokens
                continue
            self.peek += 1
            self.flushStack()
            data = None
       
        elif b in self.CHAR_NUM:
            token_type = 'NUM_INT'
            while (p:=self.nextByte()) in self.CHAR_NUM:
                if p == 46: # b'.'
                    if self.nextByte() in self.CHAR_NUM: # if decimal encountered followed by another numeric, then its a REAL (float). ex. '4.' does not need to be a float, it can be '4'
                        token_type = 'NUM_REAL'
                    else:                               # skip decimal seperator if is not followed by number since it will mess up the int evaluation (int(b'4.') does not work)
                        pop = self.popByte(2)           # pop '.{^d}'
                        self.bytes.append(pop[-1])      # return non-numeric ^d to stack
                        self.peek += 1
            self.peek += 1
            data = self.flushStack()
            data = int(bytes(data)) if token_type=='NUM_INT' else float(bytes(data))

        elif b in self.CHAR_DELIM:          
            if b==37:  # b'%':                           
                self.popByte()
                token_type = 'COMMENT'
                while self.nextByte() not in self.CHAR_EOL: continue
                self.peek += 1
                data = bytes(self.flushStack())   # save comment text, because %PDF-1.x, %bbbb, and %%EOF will tokenize as comments and we should check for them
            
            elif b==40:  # b'(':
                token_type = 'STR_LIT'
                self.popByte()                 # pop '(' delim
                n=1
                while n>0:
                    p = self.nextByte()     
                    if p==92:  # b'\\':            # '\' = 0x5c=92 escape character
                        self.nextByte()  
                    elif p==40:  # b'(':           # handle balanced unescaped paranthesis in string
                        n += 1
                    elif p==41:  # b')':
                        n -= 1     
                self.popByte()                 # pop ')' delim
                data = bytes(self.flushStack())
            
            elif b==47:  # b'/':
                self.popByte()
                token_type = 'NAME'           
                while self.nextByte() not in self.CHAR_NONREG: continue
                self.peek += 1
                data = bytes(self.flushStack())
            
            elif b==60:  # b'<':
                self.popByte()
                if self.nextByte()==60:  # b'<':         # dict_begin '<<' token
                    token_type = 'DICT_BEGIN'
                    self.popByte()
                    data = None
                else:
                    token_type = 'STR_HEX'
                    while not self.nextByte()!=62:  # b'>': 
                        continue
                    self.popByte()
                    data = bytes(self.flushStack())
                        
            elif b==62:  # b'>':
                self.popByte()
                if self.nextByte()==62:  # b'>':
                    token_type = 'DICT_END'
                    self.popByte()
                    data = None
                else:
                    self.peek += 1
                    print(f'error at pos {pos}: single > when >> expected')
                    return None
                
            elif b==91:  # b'[':
                self.popByte()
                token_type = 'ARR_BEGIN'
                data=None
            elif b==93:  # b']':
                self.popByte()
                token_type = 'ARR_END'
                data=None
                
            elif b==123:  # b'{':
                self.popByte()
                token_type = 'FN_BEGIN'
                data=None
            elif b==125:  # b'}':
                self.popByte()
                token_type = 'FN_END'
                data = None  
                        
            else:
                print(f'unhandled delim {b}')
                token_type = 'DELIM'
                data = self.flushStack()
     
        else: # not whitespace, numeric, or delim. scan for regular chars
            while self.nextByte() not in self.CHAR_NONREG: continue
            
            self.peek += 1         
            keyword = bytes(self.flushStack())
            
            if keyword == b'R':
                token_type = 'OBJ_REF'
                data=None
            elif keyword == b'n':
                token_type = 'XREF_INUSE'
                data = None
            elif keyword == b'obj':
                token_type = 'OBJ_BEGIN'
                data = None
            elif keyword == b'endobj':
                token_type = 'OBJ_END'
                data=None
            elif keyword == b'stream':
                token_type = 'STREAM'
                stream=True
                self.nextByte()  # 'stream' shall be followed by newline
                self.popByte()
                while stream:  
                    if self.nextByte() in self.CHAR_EOL:                   
                        while self.nextByte() in self.CHAR_EOL: continue
                        if self.bytes[-1]==101:                    # 101 = ord(b'e')
                            if bytes(self.nextBytes(8)) == b'ndstream':  
                                self.popByte(10)  # pop '\nendstream' from stack
                                data = bytes(self.flushStack())
                                # data = (len(data),data)  # for checking consistency with /Length data preceeding string
                                stream = False
                            else:
                                self.peek += 8
            elif keyword == b'null':
                token_type = 'NULL'
                data = None
            elif keyword == b'false':
                token_type = 'BOOL'
                data = False
            elif keyword == b'true':
                token_type = 'BOOL'
                data = True
            elif keyword == b'xref':
                token_type = 'XREF_BEGIN'
                data = None
                self.xrefLoc = pos
            elif keyword == b'f':
                token_type = 'XREF_FREE'
                data = None
            elif keyword == b'trailer':
                token_type = 'TRAILER'
                data = None
            elif keyword == b'startxref':
                token_type = 'XREF_LOC'
                data = None    
            else:
                print(f'unhandled keyword {keyword}')
                token_type = 'REG'  # should never get here.
        return self.newToken(token_type, data, pos)
    
    # @profile    
    def nextByte(self):       # peek bool allows us to read the next byte without inc counters etc..         
                                # code duplication. but, large speed increase from unrolling for loop in 
        # return getByte()
        if self.peek>0:                  # most common n=1 case and returning raw byte (int) rather than bytes object 
            b=self.bytes[-1*self.peek]   # ~2MB/s -> ~3MB/s
            self.peek -= 1
        else:
            try:                         # try/except is faster than checking for none value from iterator ('next(reader,None)')
                b=next(self.reader)
                self.bytes.append(b)
                self.pos += 1
                if b in self.CHAR_EOL:
                    self.line += 1
                    if b==13:     # 13=b'\r'. count /r{n} as 'n' lines. /r{n}/r/n counts as 'n'+1.   
                        while True:
                            bn=next(self.reader)
                            self.bytes.append(bn)
                            self.pos += 1
                            self.peek += 1
                            if bn==13:  # b'\r'
                                self.line += 1
                            else:
                                break
            except StopIteration:
                print('EOF exception') 
                self.EOF = True
                b = None           
        return b                    # returning raw byte b instead of bytes([b]) or bytes(bs) 
                                    # and converting to bytes where needed gives >4MB/s!!
           
    def nextBytes(self,n):  # seperating nextBye(n=1) and nextByte(n=8) gives 10% speed boost 
        bs=[]
        for _ in range(n):          # this loop works for case n=1 also. 
            if self.peek>0:         # but there is overhead in creating a 1-iteration loop.
                bs.append(self.bytes[-1*self.peek])
                self.peek -= 1
            else:  
                try: 
                    b=next(self.reader)
                    bs.append(b)
                    self.bytes.append(b)
                    self.pos += 1
                    if b in self.CHAR_EOL:
                        self.line += 1
                        if b==13:     # 13=b'\r'. count /r{n} as 'n' lines. /r{n}/r/n counts as 'n'+1.   
                            while True:
                                bn=next(self.reader)
                                self.bytes.append(bn)
                                self.pos += 1
                                self.peek += 1
                                if bn==13:  # b'\r'
                                    self.line += 1
                                else:
                                    break
                except Exception as e:
                    print(f'EOF exception {e}') 
                    self.EOF = True
                    break
        return bs
        
    
##############################################################


file = 'engine_pyCopy.pdf'


interp = PdfInterpreter(file)


start=time.time()
i=0
while interp.nextToken():
    i+=1
    if not i%50000:
        print(f'{time.time()-start:0.1f}s, pos={interp.pos}')
    pass
# interp.tokenize()
end=time.time()
print(f'read {interp.pos+1} bytes in {end-start:0.1f}s, {(interp.pos+1)/(1024*1024)/(end-start):0.2f}MB/s')

 


# TODO
# OK handle '<<' and '>>' delimiters OK
# OK handle hex string OK
# token parser:
    # convert <int> <int> <obj|R> to object token  (in parser)
        # handle 'endobj'
    # build nested arrays and dicts
# once parsed, write back to PDF file. successful if we can open as normal!
# gui for displaying hierarchy of doc objects
    # check boxes to include/exclude, then recompile PDF
# syntax error handling according to spec
# OK figure out whats so inefficient in this code!! it takes for fricken ever.
    # change from string appends to array appends sped up code like 1million times, now we are at 1.2mb/s from 0.04mb/s
    # beggest ineefficiency is the constant peeking, which is more expensive than reading. retool algorithm to avoid peaking
# OK handle ']' delim. doesnt work if name is last element etc [... /Name] makes a single name taken value='Name]' instead of two tokens 'Name' and ']'

# convert obj,endobj keywords  OBJ_BEGIN OBJ_END tokens

    
# TOKEN LIST:
    # regular:
        # NUM_INT
        # NUM_REAL
        # STR_LIT
        # STR_HEX
        # STREAM
    # delimiters:        
        # ARR_BEGIN
        # ARR_END
        # DICT_BEGIN
        # DICT_END
    # whitespace:
        # WS
        # WS_EOL

# OK we are bogged down. must move forward with parsing
    # validate lexer before parsing:
            # create 'unit test' file of edge cases to tokenize
                # make sure all tokens/delims are successfully detected
                
                
# according to lineprofiler there don't appear to be any more bottle necks
# we are running at 2.63MB/s for tokenizing PDF data. this may be the fastest we can go
# over 90% of time taken by byte read operation so improvements here could help
# we havent yet tried just reading the whole file at once vs iterating through it bytewise in chunks, this might be quick
    # gives 2.87MB/s. saves 6 sec on 200mb file. I dont think this is gonna get faster..
    # last thing.... try some cython for shits.



# in the future, work on a rendering system
    # need to handle operators in this case
    
    
# THINGS I DID TO IMPROVE SPEED (<1MB/s -> 5MB/s):
    # dont append to strings
    # only convert type when neccesary
    # compare with integers
    # only read once (don't peek everything)
    # reading whole file vs lazy read
    # unroll for loop when 90% of them are only 1 loop
    # minimize compares
        # ie. dont check for EOF on every byte, just write it smarter so we dont have to
    

        # BEST SPEED SO FAR 5.67MB/s
            
        